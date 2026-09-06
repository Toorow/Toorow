"""The generic Master Data owner: registries, versions, hierarchy and used-by.

Story 48.2 needed a governed home for Country, Markets, Regions and Rest of
World. Its own Implementation Gate forbids building one *for geography*: the
owner has to be the generic object model, or the next capability rebuilds it.

So nothing in this module knows what a country is. It knows:

* a **registry** -- one stable owner per (Project, ``object_kind``), carrying
  three distinct version pointers, because a failed publication that silently
  moves ``current`` is indistinguishable from one that worked;
* a **node** -- a stable identity whose *label* may change. Renaming a Market
  used to break every binding to it, because the label was the identity;
* a **version** -- editable while ``draft``, frozen the moment it is not. A
  regroup is a new version, never a mutation nobody can date;
* a **membership** -- an edge that belongs to a version, carrying effective
  dates and display order;
* **used-by** -- who depends on a node. The predecessor
  (``app.market_bindings``) returned an empty tuple when its read failed, so an
  outage read as "nothing depends on this" and let a destructive change
  through. :func:`fetch_used_by` raises instead, and every mutation guard here
  is written so that an unreadable dependency store *blocks*.

Callers own the transaction: a mutation and its audit evidence commit together.

Country mounts on this in :mod:`core.country_registry`. Business Domains,
Competitors and the rest of the Epic 49 object set mount on the same five
tables -- that is the point, and the reason ``object_kind`` and ``node_kind``
are opaque strings validated for shape and never compared to a literal here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from ulid import ULID

# ---------------------------------------------------------------------------
# Errors. The distinction that matters is between "you asked for something
# invalid" (ValueError family, a 4xx) and "the evidence required to decide is
# unreadable" (MasterDataUnavailable, which must never become an empty answer).
# ---------------------------------------------------------------------------


class MasterDataError(ValueError):
    """A governed Master Data operation was rejected."""

    code = "invalid_master_data_operation"


class MasterDataNotFound(MasterDataError):
    """The addressed registry, node or version does not exist in this Project."""

    code = "master_data_not_found"


class MasterDataConflict(MasterDataError):
    """The operation contradicts an invariant the model guarantees."""

    code = "master_data_conflict"


class MasterDataUnavailable(RuntimeError):
    """Evidence required to decide is unreadable; the caller must fail closed.

    This exists so that a used-by, alias or dependency outage can never be
    mistaken for "no dependents". It is deliberately *not* a subclass of
    :class:`MasterDataError`: a caller catching invalid input must not swallow
    an outage as well.
    """

    code = "master_data_evidence_unavailable"


_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_OPEN_ENDED = date(1, 1, 1)

REGISTRY_STATES = ("draft", "pending", "active", "disabled")
#: Lifecycle states whose registry is listed in Master Data navigation (AC1).
NAVIGABLE_STATES = frozenset({"draft", "pending", "active"})
VERSION_STATUSES = ("draft", "candidate", "current", "superseded")

_REGISTRY_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "object_kind",
    "label",
    "lifecycle_state",
    "current_version_id",
    "pending_version_id",
    "last_known_good_version_id",
    "created_by",
    "created_at",
    "updated_at",
    # Story 68.1: the business identity field a governed declaration names.
    # Read everywhere the registry is, so a replay can be told from a conflict.
    "canonical_key",
    # Migration 143 added both, and this tuple was never told. So `version_scope`
    # -- the column that decides whether a version belongs to the grouping or to
    # one identity -- was WRITTEN by `create_org_registry` and unreadable from
    # every function here, while `governance_read_model` selected it by hand two
    # modules away. A declaration its own reader cannot see is a declaration
    # nobody can check.
    "scope",
    "version_scope",
)
_NODE_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "registry_id",
    "node_kind",
    "label",
    "origin_preset_version_id",
    "archived_at",
    "created_by",
    "created_at",
    "updated_at",
)
_VERSION_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "registry_id",
    "version_number",
    "status",
    "vocabulary_version_id",
    "payload",
    "origin_preset_version_id",
    "used_by_snapshot",
    "alias_version_fingerprint",
    "content_hash",
    "effective_date",
    "created_by",
    "created_at",
    "published_at",
    "published_by",
)


# ---------------------------------------------------------------------------
# Canonical hashing -- the same implementation Story 48.1 pins proposals with,
# so a REST body and an MCP payload describing one version hash identically.
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise MasterDataError("master data content must be JSON serializable") from exc


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MasterDataError(f"{label} is required")
    return value.strip()


def _kind(value: Any, label: str) -> str:
    text = _required(value, label)
    if not _KIND_RE.fullmatch(text):
        raise MasterDataError(f"{label} must be lowercase snake_case, 2 to 40 characters")
    return text


def _label(value: Any, label: str) -> str:
    text = _required(value, label)
    if len(text) > 120:
        raise MasterDataError(f"{label} must be 120 characters or fewer")
    return text


def _row(row: Any, columns: Sequence[str], what: str) -> dict[str, Any]:
    if row is None:
        raise MasterDataNotFound(f"{what} not found")
    return dict(zip(columns, row, strict=False))


def _as_date(value: Any, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise MasterDataError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


# ---------------------------------------------------------------------------
# Pure hierarchy model. Everything here is testable without a database, which
# is what lets the invariants of AC3 be proven directly instead of inferred
# from a persisted example.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Membership:
    """One hierarchy edge inside one version."""

    parent_node_id: str
    child_node_id: str | None = None
    child_value: str | None = None
    effective_from: date = _OPEN_ENDED
    effective_to: date | None = None
    display_order: int = 0

    def __post_init__(self) -> None:
        if (self.child_node_id is None) == (self.child_value is None):
            raise MasterDataError(
                "a membership names exactly one child: a node or a vocabulary value"
            )
        if self.child_node_id is not None and self.child_node_id == self.parent_node_id:
            raise MasterDataConflict("a node cannot contain itself")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise MasterDataError("effective_to must be after effective_from")

    @property
    def child_key(self) -> tuple[str, str]:
        if self.child_node_id is not None:
            return ("node", self.child_node_id)
        return ("value", str(self.child_value))

    def overlaps(self, other: "Membership") -> bool:
        start = max(self.effective_from, other.effective_from)
        end_self = self.effective_to or date.max
        end_other = other.effective_to or date.max
        return start < min(end_self, end_other)

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent_node_id": self.parent_node_id,
            "child_node_id": self.child_node_id,
            "child_value": self.child_value,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "display_order": self.display_order,
        }


def membership_from_mapping(value: Mapping[str, Any]) -> Membership:
    return Membership(
        parent_node_id=_required(value.get("parent_node_id"), "parent_node_id"),
        child_node_id=(value.get("child_node_id") or None),
        child_value=(str(value["child_value"]).strip() or None)
        if value.get("child_value") not in (None, "")
        else None,
        effective_from=_as_date(value.get("effective_from"), "effective_from") or _OPEN_ENDED,
        effective_to=_as_date(value.get("effective_to"), "effective_to"),
        display_order=int(value.get("display_order") or 0),
    )


@dataclass(frozen=True, slots=True)
class HierarchyValidation:
    """What a draft would be if it were published, and why it may not be."""

    errors: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()
    depth: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_hierarchy(
    memberships: Iterable[Membership],
    *,
    known_node_ids: Iterable[str],
    known_values: Iterable[str] | None = None,
) -> HierarchyValidation:
    """Fail closed on cycles, duplicate effective membership and unknown members.

    "At most one effective Market and one effective Region for a given version
    and date" (AC3) is exactly the duplicate-overlap rule below: two edges that
    claim the same child over intersecting date ranges are rejected, whichever
    parents they name. An additive total cannot be proven otherwise -- a country
    counted under two Markets double-counts, which is the failure the whole
    reconciliation of AC5 exists to prevent.
    """

    nodes = set(known_node_ids)
    values = set(known_values) if known_values is not None else None
    edges = list(memberships)
    errors: list[str] = []

    by_child: dict[tuple[str, str], list[Membership]] = {}
    children_of: dict[str, list[str]] = {}
    child_nodes: set[str] = set()
    holds_values: set[str] = set()

    for edge in edges:
        if edge.parent_node_id not in nodes:
            errors.append(f"unknown parent node: {edge.parent_node_id}")
            continue
        if edge.child_node_id is not None:
            if edge.child_node_id not in nodes:
                errors.append(f"unknown child node: {edge.child_node_id}")
                continue
            children_of.setdefault(edge.parent_node_id, []).append(edge.child_node_id)
            child_nodes.add(edge.child_node_id)
        elif values is not None and edge.child_value not in values:
            errors.append(f"value outside the pinned vocabulary: {edge.child_value}")
            continue
        else:
            holds_values.add(edge.parent_node_id)
        by_child.setdefault(edge.child_key, []).append(edge)

    for key, group in sorted(by_child.items()):
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                if first.overlaps(second):
                    kind, identity = key
                    errors.append(
                        f"{identity} has two effective parents over the same dates"
                        if kind == "value"
                        else f"node {identity} has two effective parents over the same dates"
                    )
                    break

    # Cycle detection over node-to-node edges only: a vocabulary value is a leaf
    # by construction and cannot close a loop.
    colour: dict[str, int] = {}
    cyclic = False

    def visit(node: str, depth: int) -> int:
        nonlocal cyclic
        state = colour.get(node, 0)
        if state == 1:
            cyclic = True
            return depth
        if state == 2:
            return depth
        colour[node] = 1
        # Depth is what the editor renders, so a vocabulary leaf counts: a
        # Region holding a Market holding countries is three levels deep, not
        # two, and the tree that shows it has to reserve the room.
        deepest = depth + 1 if node in holds_values else depth
        for child in children_of.get(node, ()):
            deepest = max(deepest, visit(child, depth + 1))
        colour[node] = 2
        return deepest

    roots = sorted(node for node in nodes if node not in child_nodes)
    depth = 0
    for root in roots:
        depth = max(depth, visit(root, 1))
    for node in sorted(nodes):
        if colour.get(node, 0) == 0:
            visit(node, 1)
    if cyclic:
        errors.append("the hierarchy contains a cycle")

    # Deduplicate while keeping the first-seen order: a caller shows this list.
    seen: set[str] = set()
    ordered = [item for item in errors if not (item in seen or seen.add(item))]
    return HierarchyValidation(errors=tuple(ordered), roots=tuple(roots), depth=depth)


def effective_parent(
    memberships: Iterable[Membership], child_key: tuple[str, str], as_of: date
) -> str | None:
    """The single parent a child has on *as_of*, or None when unassigned."""

    for edge in memberships:
        if edge.child_key != child_key:
            continue
        if edge.effective_from <= as_of and (
            edge.effective_to is None or as_of < edge.effective_to
        ):
            return edge.parent_node_id
    return None


def resolve_ancestry(
    memberships: Sequence[Membership], node_id: str, as_of: date
) -> tuple[str, ...]:
    """The chain from *node_id* up to its root on *as_of*, innermost first."""

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None and current not in seen:
        chain.append(current)
        seen.add(current)
        current = effective_parent(memberships, ("node", current), as_of)
    return tuple(chain)


# ---------------------------------------------------------------------------
# Registries.
# ---------------------------------------------------------------------------


def create_registry(
    conn,
    *,
    org_id: str,
    project_id: str,
    object_kind: str,
    label: str,
    actor: str,
    version_scope: str = "registry",
    canonical_key: str | None = None,
) -> dict[str, Any]:
    """Mint the single owner for one object kind inside one Project.

    ``version_scope`` was previously left to the column default and therefore
    unreachable from this door (Story 64.13). Its twin ``create_org_registry`` has
    always taken it, so a project-scoped registry could not do what an org-scoped
    one could -- and the difference decides where the payload lives:

      ``registry``  ONE version, and one ``payload``, for the whole grouping.
                    Country's shape: the hierarchy publishes as one act.
      ``node``      EVERY identity has its own version and its own ``payload``.
                    The shape a client object needs: each video, each product
                    carries its own properties.

    The default stays ``registry`` so `country_registry`, the only caller until
    this change, keeps its behaviour exactly. A caller that wants per-instance
    properties now has to say so, which is the right way round.

    ``canonical_key`` (Story 68.1) is the business identity field a governed
    entity-type declaration names. Optional here because Country and the
    feeding-source declarations of Story 64.1 never named one; the feeder-less
    declaration door (:func:`core.object_kind_registry.declare_entity_type`)
    always passes it.
    """

    if version_scope not in (VERSION_SCOPE_REGISTRY, VERSION_SCOPE_NODE):
        raise MasterDataError(f"unknown version scope: {version_scope!r}")

    registry_id = _mint("mdreg")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_registries
                (id, org_id, project_id, object_kind, label, version_scope, created_by,
                 canonical_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, object_kind) DO NOTHING
            RETURNING {", ".join(_REGISTRY_COLUMNS)}
            """,
            (
                registry_id,
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
                _kind(object_kind, "object_kind"),
                _label(label, "label"),
                version_scope,
                _required(actor, "actor"),
                _kind(canonical_key, "canonical_key") if canonical_key is not None else None,
            ),
        )
        row = cur.fetchone()
    if row is not None:
        return _row(row, _REGISTRY_COLUMNS, "registry")
    existing = fetch_registry(conn, project_id=project_id, object_kind=object_kind)
    if existing is None:  # pragma: no cover - only under a concurrent delete
        raise MasterDataConflict("registry could not be created or read back")
    return existing


def fetch_registry(
    conn,
    *,
    project_id: str,
    object_kind: str | None = None,
    registry_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one registry by kind or id. Returns None when it does not exist."""

    if object_kind is None and registry_id is None:
        raise MasterDataError("fetch_registry requires object_kind or registry_id")
    clause = "object_kind = %s" if registry_id is None else "id = %s"
    needle = _kind(object_kind, "object_kind") if registry_id is None else _required(
        registry_id, "registry_id"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_REGISTRY_COLUMNS)}
            FROM app.master_data_registries
            WHERE project_id = %s AND {clause}
            """,
            (_required(project_id, "project_id"), needle),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_REGISTRY_COLUMNS, row, strict=False))


def require_registry(
    conn, *, project_id: str, object_kind: str | None = None, registry_id: str | None = None
) -> dict[str, Any]:
    registry = fetch_registry(
        conn, project_id=project_id, object_kind=object_kind, registry_id=registry_id
    )
    if registry is None:
        raise MasterDataNotFound("registry not found in this Project")
    return registry


def list_registries(conn, *, project_id: str, navigable_only: bool = True) -> list[dict[str, Any]]:
    """The registries Master Data lists. A disabled one is not navigation (AC1)."""

    clause = "AND lifecycle_state = ANY(%s)" if navigable_only else ""
    params: list[Any] = [_required(project_id, "project_id")]
    if navigable_only:
        params.append(sorted(NAVIGABLE_STATES))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_REGISTRY_COLUMNS)}
            FROM app.master_data_registries
            WHERE project_id = %s {clause}
            ORDER BY label, id
            """,
            tuple(params),
        )
        return [dict(zip(_REGISTRY_COLUMNS, row, strict=False)) for row in cur.fetchall()]


def set_registry_state(
    conn, *, project_id: str, registry_id: str, lifecycle_state: str
) -> dict[str, Any]:
    if lifecycle_state not in REGISTRY_STATES:
        raise MasterDataError(f"unknown registry lifecycle state: {lifecycle_state!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_registries
               SET lifecycle_state = %s, updated_at = NOW()
             WHERE project_id = %s AND id = %s
            RETURNING {", ".join(_REGISTRY_COLUMNS)}
            """,
            (
                lifecycle_state,
                _required(project_id, "project_id"),
                _required(registry_id, "registry_id"),
            ),
        )
        return _row(cur.fetchone(), _REGISTRY_COLUMNS, "registry")


# ---------------------------------------------------------------------------
# Vocabularies. Platform-scoped, immutable, content-addressed.
# ---------------------------------------------------------------------------


def import_vocabulary_version(
    conn,
    *,
    vocabulary_key: str,
    source_authority: str,
    source_version: str,
    effective_date: date | str,
    entries: Sequence[Mapping[str, Any]],
    actor: str,
    source_reference: str | None = None,
) -> dict[str, Any]:
    """Store one immutable snapshot, or return the identical one already stored.

    Determinism is the property that matters: re-running the deterministic
    export must not mint a rival vocabulary with identical content, or two
    hierarchy versions pinned to "the ISO set" would pin different rows.
    """

    key = _kind(vocabulary_key, "vocabulary_key")
    normalized = [dict(entry) for entry in entries]
    if not normalized:
        raise MasterDataError("a vocabulary version needs at least one entry")
    digest = content_hash(
        {
            "vocabulary_key": key,
            "source_authority": source_authority,
            "source_version": source_version,
            "entries": normalized,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, vocabulary_key, source_authority, source_version, source_reference,
                   effective_date, entries, entry_count, content_hash, created_at
            FROM app.master_data_vocabulary_versions
            WHERE vocabulary_key = %s AND content_hash = %s
            """,
            (key, digest),
        )
        row = cur.fetchone()
        if row is not None:
            return _vocabulary_dict(row)
        cur.execute(
            """
            INSERT INTO app.master_data_vocabulary_versions
                (id, vocabulary_key, source_authority, source_version, source_reference,
                 effective_date, entries, entry_count, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id, vocabulary_key, source_authority, source_version, source_reference,
                      effective_date, entries, entry_count, content_hash, created_at
            """,
            (
                _mint("mdvoc"),
                key,
                _required(source_authority, "source_authority"),
                _required(source_version, "source_version"),
                source_reference,
                _as_date(effective_date, "effective_date"),
                json.dumps(normalized),
                len(normalized),
                digest,
                _required(actor, "actor"),
            ),
        )
        return _vocabulary_dict(cur.fetchone())


_VOCABULARY_COLUMNS = (
    "id",
    "vocabulary_key",
    "source_authority",
    "source_version",
    "source_reference",
    "effective_date",
    "entries",
    "entry_count",
    "content_hash",
    "created_at",
)


def _vocabulary_dict(row: Any) -> dict[str, Any]:
    return _row(row, _VOCABULARY_COLUMNS, "vocabulary version")


def fetch_vocabulary_version(
    conn, *, vocabulary_version_id: str | None = None, vocabulary_key: str | None = None
) -> dict[str, Any] | None:
    """One snapshot by id, or the latest effective snapshot for a key."""

    with conn.cursor() as cur:
        if vocabulary_version_id:
            cur.execute(
                f"""
                SELECT {", ".join(_VOCABULARY_COLUMNS)}
                FROM app.master_data_vocabulary_versions WHERE id = %s
                """,
                (vocabulary_version_id,),
            )
        elif vocabulary_key:
            cur.execute(
                f"""
                SELECT {", ".join(_VOCABULARY_COLUMNS)}
                FROM app.master_data_vocabulary_versions
                WHERE vocabulary_key = %s
                ORDER BY effective_date DESC, created_at DESC
                LIMIT 1
                """,
                (_kind(vocabulary_key, "vocabulary_key"),),
            )
        else:
            raise MasterDataError("fetch_vocabulary_version needs an id or a key")
        row = cur.fetchone()
    return None if row is None else dict(zip(_VOCABULARY_COLUMNS, row, strict=False))


# ---------------------------------------------------------------------------
# Nodes. Identity is stable; the label is not.
# ---------------------------------------------------------------------------


def create_node(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    node_kind: str,
    label: str,
    actor: str,
    origin_preset_version_id: str | None = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_nodes
                (id, org_id, project_id, registry_id, node_kind, label,
                 origin_preset_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (
                _mint("mdnode"),
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
                _required(registry_id, "registry_id"),
                _kind(node_kind, "node_kind"),
                _label(label, "label"),
                origin_preset_version_id,
                _required(actor, "actor"),
            ),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


def rename_node(conn, *, project_id: str, node_id: str, label: str) -> dict[str, Any]:
    """Change what a node is called. Its identity, and every binding, survive."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_nodes
               SET label = %s, updated_at = NOW()
             WHERE project_id = %s AND id = %s
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (
                _label(label, "label"),
                _required(project_id, "project_id"),
                _required(node_id, "node_id"),
            ),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


def fetch_node(conn, *, project_id: str, node_id: str) -> dict[str, Any] | None:
    """Read one Project identity by id. The twin of :func:`fetch_org_node`.

    Returns None rather than raising when the id belongs to another Project, so
    the caller answers the same not-found a missing id produces -- existence
    stays hidden because the two cases are one case here.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_COLUMNS)}
            FROM app.master_data_nodes
            WHERE project_id = %s AND id = %s
            """,
            (_required(project_id, "project_id"), _required(node_id, "node_id")),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_NODE_COLUMNS, row, strict=False))


def list_nodes(
    conn, *, project_id: str, registry_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    clause = "" if include_archived else "AND archived_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_COLUMNS)}
            FROM app.master_data_nodes
            WHERE project_id = %s AND registry_id = %s {clause}
            ORDER BY node_kind, label, id
            """,
            (_required(project_id, "project_id"), _required(registry_id, "registry_id")),
        )
        return [dict(zip(_NODE_COLUMNS, row, strict=False)) for row in cur.fetchall()]


@dataclass(frozen=True, slots=True)
class NodeImpact:
    """Who still depends on a node, read before a command that would move it.

    `consumers` lists the live (not released) `app.master_data_used_by` rows.
    An empty tuple means the store answered and there are none -- it never means
    the store could not be read, because :func:`assess_node_impact` raises
    :class:`MasterDataUnavailable` in that case rather than returning an
    encouraging zero.
    """

    node_id: str
    consumers: tuple[dict[str, Any], ...]

    @property
    def is_clear(self) -> bool:
        return not self.consumers

    def describe(self) -> str:
        kinds = sorted({str(c["consumer_kind"]) for c in self.consumers})
        return ", ".join(
            f"{kind} ({sum(1 for c in self.consumers if c['consumer_kind'] == kind)})"
            for kind in kinds
        )


def assess_node_impact(conn, *, project_id: str, node_id: str) -> NodeImpact:
    """Live consumers of one node, or an outage -- never a reassuring zero.

    Raises:
        MasterDataUnavailable: the used-by store could not be read. The command
            that asked must fail closed: "I could not check" and "nothing depends
            on this" are different facts, and only one of them is safe to act on.
    """
    project_id = _required(project_id, "project_id")
    node_id = _required(node_id, "node_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT consumer_kind, consumer_id, consumer_label, consumer_version_id
                  FROM app.master_data_used_by
                 WHERE project_id = %s AND node_id = %s AND released_at IS NULL
                 ORDER BY consumer_kind, consumer_id
                """,
                (project_id, node_id),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- any read failure is an outage here
        raise MasterDataUnavailable(
            f"the used-by store could not be read for node {node_id}: {type(exc).__name__}"
        ) from exc
    consumers = tuple(
        {
            "consumer_kind": r[0],
            "consumer_id": r[1],
            "consumer_label": r[2],
            "consumer_version_id": r[3],
        }
        for r in rows
    )
    return NodeImpact(node_id=node_id, consumers=consumers)


def archive_node(
    conn,
    *,
    project_id: str,
    node_id: str,
    acknowledge_impact: bool = False,
    impact: NodeImpact | None = None,
) -> dict[str, Any]:
    """Retire an identity without deleting it: older versions still name it.

    Impact-guarded (Story 49.2). Before this guard existed, archiving a node that
    live consumers still referenced succeeded silently -- the Datastream mapping,
    Analyze filter or Context link pointing at it simply stopped resolving, with
    no record of the decision. The used-by store was already written by
    :func:`register_used_by`; nothing read it.

    `acknowledge_impact=True` is the caller stating that the consumers were shown
    to a human who chose to proceed. It is deliberately a separate argument and
    not a default: the whole point is that the decision has to be made, not
    inherited.

    `impact` lets a caller that has ALREADY assessed pass its reading in, so the
    guard runs once on one set of evidence. `core.master_data_commands` does this
    because it must show the consumers to a human before acting: re-reading here
    would be a second query and, worse, a window in which the two decisions are
    made against different answers. Omitting it reads the store here, which is
    what a direct caller wants.

    Raises:
        MasterDataConflict: live consumers exist and the impact was not acknowledged.
        MasterDataUnavailable: the used-by store could not be read (fail closed).
    """
    project_id = _required(project_id, "project_id")
    node_id = _required(node_id, "node_id")

    if impact is None:
        impact = assess_node_impact(conn, project_id=project_id, node_id=node_id)
    if not impact.is_clear and not acknowledge_impact:
        raise MasterDataConflict(
            f"node {node_id} is still used by {impact.describe()}. "
            "Archiving it would break those consumers. Release them, or archive "
            "with the impact acknowledged."
        )

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_nodes
               SET archived_at = NOW(), updated_at = NOW()
             WHERE project_id = %s AND id = %s AND archived_at IS NULL
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (project_id, node_id),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


def restore_node(conn, *, project_id: str, node_id: str) -> dict[str, Any]:
    """Bring an archived identity back under its original ID.

    The inverse of :func:`archive_node`, and it restores rather than recreates:
    a new node would mint a new ID, and every version, alias and used-by row that
    names the old one would be orphaned. Restoring needs no impact guard -- it
    adds an identity back, it removes nothing.

    Raises:
        MasterDataNotFound: no archived node with this ID in this Project. An
            already-live node is reported as not-found-archived rather than
            silently succeeding, so a caller cannot mistake "it was never gone"
            for "I brought it back".
    """
    project_id = _required(project_id, "project_id")
    node_id = _required(node_id, "node_id")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_nodes
               SET archived_at = NULL, updated_at = NOW()
             WHERE project_id = %s AND id = %s AND archived_at IS NOT NULL
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (project_id, node_id),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "archived node")


# ---------------------------------------------------------------------------
# Versions and their memberships.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftContent:
    """Everything a draft revision replaces in one write."""

    memberships: tuple[Membership, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def digest(self, *, vocabulary_version_id: str | None) -> str:
        return content_hash(
            {
                "vocabulary_version_id": vocabulary_version_id,
                "payload": dict(self.payload),
                "memberships": sorted(
                    (edge.as_dict() for edge in self.memberships),
                    key=canonical_json,
                ),
            }
        )


def create_draft_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    vocabulary_version_id: str | None = None,
    actor: str,
    content: DraftContent | None = None,
    origin_preset_version_id: str | None = None,
    effective_date: date | str | None = None,
) -> dict[str, Any]:
    """Open a new editable revision. Nothing about it is published yet."""

    body = content or DraftContent()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.master_data_object_versions
            WHERE project_id = %s AND registry_id = %s
            """,
            (_required(project_id, "project_id"), _required(registry_id, "registry_id")),
        )
        (next_number,) = cur.fetchone()
        version_id = _mint("mdver")
        cur.execute(
            f"""
            INSERT INTO app.master_data_object_versions
                (id, org_id, project_id, registry_id, version_number, status,
                 vocabulary_version_id, payload, origin_preset_version_id,
                 content_hash, effective_date, created_by)
            VALUES (%s, %s, %s, %s, %s, 'draft', %s, %s::jsonb, %s, %s, %s, %s)
            RETURNING {", ".join(_VERSION_COLUMNS)}
            """,
            (
                version_id,
                _required(org_id, "org_id"),
                project_id,
                registry_id,
                next_number,
                vocabulary_version_id,
                json.dumps(dict(body.payload)),
                origin_preset_version_id,
                body.digest(vocabulary_version_id=vocabulary_version_id),
                _as_date(effective_date, "effective_date"),
                _required(actor, "actor"),
            ),
        )
        version = _row(cur.fetchone(), _VERSION_COLUMNS, "version")
    if body.memberships:
        replace_draft_memberships(
            conn, project_id=project_id, version_id=version_id, memberships=body.memberships
        )
        version = require_version(conn, project_id=project_id, version_id=version_id)
    return version


def replace_draft_memberships(
    conn, *, project_id: str, version_id: str, memberships: Sequence[Membership]
) -> dict[str, Any]:
    """Rewrite a draft's edges wholesale and re-derive its content hash.

    Validation runs before the write, against the nodes and vocabulary the draft
    actually pins -- so an invalid draft never exists, not even briefly.
    """

    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version["status"] != "draft":
        raise MasterDataConflict("only a draft revision can be edited")

    nodes = list_nodes(
        conn, project_id=project_id, registry_id=version["registry_id"], include_archived=True
    )
    # A version that pins a vocabulary is checked against it. A version that
    # pins none has no controlled value set to check against -- that is a legal
    # object kind (a pure node hierarchy), not a check silently skipped.
    values: set[str] | None = None
    if version["vocabulary_version_id"]:
        vocabulary = fetch_vocabulary_version(
            conn, vocabulary_version_id=version["vocabulary_version_id"]
        )
        if vocabulary is None:
            raise MasterDataUnavailable("the pinned vocabulary version is unreadable")
        values = {str(entry.get("code")) for entry in vocabulary["entries"]}
    outcome = validate_hierarchy(
        memberships,
        known_node_ids=[node["id"] for node in nodes],
        known_values=values,
    )
    if not outcome.ok:
        raise MasterDataConflict("; ".join(outcome.errors))

    digest = DraftContent(
        memberships=tuple(memberships), payload=version["payload"] or {}
    ).digest(vocabulary_version_id=version["vocabulary_version_id"])

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.master_data_memberships WHERE project_id = %s AND version_id = %s",
            (project_id, version_id),
        )
        for edge in memberships:
            cur.execute(
                """
                INSERT INTO app.master_data_memberships
                    (id, project_id, version_id, parent_node_id, child_node_id, child_value,
                     effective_from, effective_to, display_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _mint("mdmem"),
                    project_id,
                    version_id,
                    edge.parent_node_id,
                    edge.child_node_id,
                    edge.child_value,
                    edge.effective_from,
                    edge.effective_to,
                    edge.display_order,
                ),
            )
        cur.execute(
            f"""
            UPDATE app.master_data_object_versions
               SET content_hash = %s
             WHERE project_id = %s AND id = %s
            RETURNING {", ".join(_VERSION_COLUMNS)}
            """,
            (digest, project_id, version_id),
        )
        return _row(cur.fetchone(), _VERSION_COLUMNS, "version")


def update_draft_payload(
    conn, *, project_id: str, version_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace the non-edge content of a draft (labels, policies, references)."""

    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version["status"] != "draft":
        raise MasterDataConflict("only a draft revision can be edited")
    edges = fetch_memberships(conn, project_id=project_id, version_id=version_id)
    digest = DraftContent(memberships=edges, payload=dict(payload)).digest(
        vocabulary_version_id=version["vocabulary_version_id"]
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_object_versions
               SET payload = %s::jsonb, content_hash = %s
             WHERE project_id = %s AND id = %s
            RETURNING {", ".join(_VERSION_COLUMNS)}
            """,
            (json.dumps(dict(payload)), digest, project_id, version_id),
        )
        return _row(cur.fetchone(), _VERSION_COLUMNS, "version")


def fetch_memberships(conn, *, project_id: str, version_id: str) -> tuple[Membership, ...]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT parent_node_id, child_node_id, child_value,
                   effective_from, effective_to, display_order
            FROM app.master_data_memberships
            WHERE project_id = %s AND version_id = %s
            ORDER BY parent_node_id, display_order, id
            """,
            (_required(project_id, "project_id"), _required(version_id, "version_id")),
        )
        return tuple(
            Membership(
                parent_node_id=row[0],
                child_node_id=row[1],
                child_value=row[2],
                effective_from=row[3],
                effective_to=row[4],
                display_order=row[5],
            )
            for row in cur.fetchall()
        )


def require_version(conn, *, project_id: str, version_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_VERSION_COLUMNS)}
            FROM app.master_data_object_versions
            WHERE project_id = %s AND id = %s
            """,
            (_required(project_id, "project_id"), _required(version_id, "version_id")),
        )
        return _row(cur.fetchone(), _VERSION_COLUMNS, "version")


def list_versions(conn, *, project_id: str, registry_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_VERSION_COLUMNS)}
            FROM app.master_data_object_versions
            WHERE project_id = %s AND registry_id = %s
            ORDER BY version_number DESC
            """,
            (_required(project_id, "project_id"), _required(registry_id, "registry_id")),
        )
        return [dict(zip(_VERSION_COLUMNS, row, strict=False)) for row in cur.fetchall()]


def mark_candidate(conn, *, project_id: str, version_id: str) -> dict[str, Any]:
    """Freeze a draft for review. It is not published, and nothing points at it yet."""

    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version["status"] != "draft":
        raise MasterDataConflict("only a draft revision becomes a candidate")
    snapshot = used_by_snapshot(conn, project_id=project_id, registry_id=version["registry_id"])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_object_versions
               SET status = 'candidate', used_by_snapshot = %s::jsonb
             WHERE project_id = %s AND id = %s
            RETURNING {", ".join(_VERSION_COLUMNS)}
            """,
            (json.dumps(snapshot), project_id, version_id),
        )
        version = _row(cur.fetchone(), _VERSION_COLUMNS, "version")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.master_data_registries
               SET pending_version_id = %s, lifecycle_state =
                   CASE WHEN lifecycle_state = 'draft' THEN 'pending' ELSE lifecycle_state END,
                   updated_at = NOW()
             WHERE project_id = %s AND id = %s
            """,
            (version_id, project_id, version["registry_id"]),
        )
    return version


def publish_version(
    conn, *, project_id: str, version_id: str, actor: str, expected_content_hash: str | None = None
) -> dict[str, Any]:
    """Make a candidate current, atomically, or leave everything as it was.

    The previous current version becomes last-known-good *before* it is
    superseded, so a rollback always has somewhere to go. Nothing about a plan,
    a mapping, an execution or a publication pointer moves here: publishing a
    governed meaning is not publishing data (AC6).
    """

    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version["status"] not in ("draft", "candidate"):
        raise MasterDataConflict("only a draft or candidate revision can be published")
    if expected_content_hash and expected_content_hash != version["content_hash"]:
        raise MasterDataConflict(
            "the revision changed since it was reviewed; re-prepare before publishing"
        )
    registry = require_registry(conn, project_id=project_id, registry_id=version["registry_id"])
    previous_current = registry["current_version_id"]

    with conn.cursor() as cur:
        if previous_current and previous_current != version_id:
            cur.execute(
                """
                UPDATE app.master_data_object_versions
                   SET status = 'superseded'
                 WHERE project_id = %s AND id = %s AND status = 'current'
                """,
                (project_id, previous_current),
            )
        cur.execute(
            f"""
            UPDATE app.master_data_object_versions
               SET status = 'current', published_at = NOW(), published_by = %s
             WHERE project_id = %s AND id = %s
            RETURNING {", ".join(_VERSION_COLUMNS)}
            """,
            (_required(actor, "actor"), project_id, version_id),
        )
        published = _row(cur.fetchone(), _VERSION_COLUMNS, "version")
        cur.execute(
            """
            UPDATE app.master_data_registries
               SET current_version_id = %s,
                   last_known_good_version_id = COALESCE(%s, last_known_good_version_id),
                   pending_version_id = NULL,
                   lifecycle_state = 'active',
                   updated_at = NOW()
             WHERE project_id = %s AND id = %s
            """,
            (version_id, previous_current, project_id, registry["id"]),
        )
    return published


# ---------------------------------------------------------------------------
# Used-by. The guard that must never turn an outage into an empty list.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsedByReference:
    """One live dependency, with everything a reader needs to act on it.

    ``workspace`` and ``recorded_at`` are READ-ONLY facts: no writer supplies
    them and no column stores them. Story 49.2 AC7 requires each reference to
    carry *"owner workspace, stable consumer ID, pinned version when applicable,
    evidence time and an exact owner link"*, and a bare ``consumer_kind`` says
    none of it -- which is how the workbench came to render "Nothing depends on
    this object" for an identity with three live dependents.

    The workspace is DERIVED (``core.master_data_consumers``) rather than
    stored, because migration 140 refuses to let this store enumerate its
    consumers: *"enumerating consumers here would make the platform decide which
    surfaces are allowed to depend on Master Data"*. A kind the derivation does
    not know keeps ``workspace = None``, which the reader reports as an
    unnamed owner -- never as a workspace it guessed.
    """

    node_id: str
    consumer_kind: str
    consumer_id: str
    consumer_label: str | None = None
    consumer_version_id: str | None = None
    hierarchy_version_id: str | None = None
    #: Read-side only. The owner workspace this consumer lives in, or None.
    workspace: str | None = None
    #: Read-side only. When the dependency was recorded -- AC7's evidence time.
    recorded_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "consumer_kind": self.consumer_kind,
            "consumer_id": self.consumer_id,
            "consumer_label": self.consumer_label,
            "consumer_version_id": self.consumer_version_id,
            "hierarchy_version_id": self.hierarchy_version_id,
            "workspace": self.workspace,
            "recorded_at": self.recorded_at,
        }


def register_used_by(
    conn,
    *,
    project_id: str,
    registry_id: str,
    reference: UsedByReference,
    actor: str = "system",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.master_data_used_by
                (id, project_id, registry_id, node_id, consumer_kind, consumer_id,
                 consumer_label, consumer_version_id, hierarchy_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, node_id, consumer_kind, consumer_id,
                         COALESCE(consumer_version_id, ''))
            DO UPDATE SET consumer_label = EXCLUDED.consumer_label,
                          hierarchy_version_id = EXCLUDED.hierarchy_version_id,
                          released_at = NULL
            """,
            (
                _mint("mduse"),
                _required(project_id, "project_id"),
                _required(registry_id, "registry_id"),
                _required(reference.node_id, "node_id"),
                _required(reference.consumer_kind, "consumer_kind"),
                _required(reference.consumer_id, "consumer_id"),
                reference.consumer_label,
                reference.consumer_version_id,
                reference.hierarchy_version_id,
                _required(actor, "actor"),
            ),
        )


def release_used_by(
    conn,
    *,
    project_id: str,
    node_id: str | None = None,
    consumer_kind: str,
    consumer_id: str,
    consumer_version_id: str | None = None,
) -> None:
    """Release live dependencies of one consumer.

    ``node_id`` narrows to a single governed node; omit it to release every node
    this consumer depended on. ``consumer_version_id`` narrows to ONE version of
    that consumer, which is what a supersession needs: when a Rule Set publishes a
    new version, the version it replaced genuinely no longer depends on anything,
    but the new one may depend on the very same market -- so releasing by consumer
    alone would drop the dependency that was just declared.
    """

    clauses = ["project_id = %s", "consumer_kind = %s", "consumer_id = %s", "released_at IS NULL"]
    params: list[Any] = [project_id, consumer_kind, consumer_id]
    if node_id is not None:
        clauses.append("node_id = %s")
        params.append(node_id)
    if consumer_version_id is not None:
        clauses.append("consumer_version_id = %s")
        params.append(consumer_version_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.master_data_used_by SET released_at = NOW() WHERE "
            + " AND ".join(clauses),
            tuple(params),
        )


def fetch_used_by(
    conn, *, project_id: str, registry_id: str, node_ids: Sequence[str] | None = None
) -> tuple[UsedByReference, ...]:
    """Every live dependent, or an exception. **Never** an empty tuple on failure.

    ``app.market_bindings``'s reader swallowed its errors and returned ``()``,
    which a mutation guard read as "nothing depends on this market". That is why
    this function converts any read failure into :class:`MasterDataUnavailable`
    and why every caller below treats that as a hard block.
    """

    clause = "" if not node_ids else "AND node_id = ANY(%s)"
    params: list[Any] = [_required(project_id, "project_id"), _required(registry_id, "registry_id")]
    if node_ids:
        params.append(list(node_ids))
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT node_id, consumer_kind, consumer_id, consumer_label,
                       consumer_version_id, hierarchy_version_id, created_at
                FROM app.master_data_used_by
                WHERE project_id = %s AND registry_id = %s AND released_at IS NULL {clause}
                ORDER BY consumer_kind, consumer_id
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- any read failure must fail closed
        raise MasterDataUnavailable("the used-by store is unreadable") from exc
    from core.master_data_consumers import consumer_workspace  # noqa: PLC0415 -- read-side only

    return tuple(
        UsedByReference(
            node_id=row[0],
            consumer_kind=row[1],
            consumer_id=row[2],
            consumer_label=row[3],
            consumer_version_id=row[4],
            hierarchy_version_id=row[5],
            workspace=consumer_workspace(row[1]),
            recorded_at=None if row[6] is None else row[6].isoformat(),
        )
        for row in rows
    )


def used_by_snapshot(conn, *, project_id: str, registry_id: str) -> list[dict[str, Any]]:
    """The dependents frozen into a version at review time."""

    references = fetch_used_by(conn, project_id=project_id, registry_id=registry_id)
    return [ref.as_dict() for ref in references]


#: How many nodes and versions a summary carries. A host that needs the whole
#: hierarchy opens the owner link; a payload that silently truncates would read
#: as the complete picture, so :func:`registry_summary` states what it dropped.
SUMMARY_NODE_LIMIT = 50
SUMMARY_VERSION_LIMIT = 10


def registry_summary(
    conn, *, project_id: str, object_kind: str
) -> dict[str, Any] | None:
    """A bounded, safe projection of one registry for a read-only consumer.

    Returns ``None`` when the Project has no registry of that kind -- a real
    answer, distinct from an empty one. Carries no raw source values and no
    confirmation material: this payload is model-visible, and AD-27 forbids both.
    """

    registry = fetch_registry(conn, project_id=project_id, object_kind=object_kind)
    if registry is None:
        return None
    nodes = list_nodes(conn, project_id=project_id, registry_id=registry["id"])
    versions = list_versions(conn, project_id=project_id, registry_id=registry["id"])
    current = registry["current_version_id"]
    member_counts: dict[str, int] = {}
    if current:
        for edge in fetch_memberships(conn, project_id=project_id, version_id=str(current)):
            member_counts[edge.parent_node_id] = member_counts.get(edge.parent_node_id, 0) + 1
    return {
        "schema": "master_data_registry_summary.v1",
        "registry_id": registry["id"],
        "object_kind": registry["object_kind"],
        "label": registry["label"],
        "lifecycle_state": registry["lifecycle_state"],
        "current_version_id": current,
        "pending_version_id": registry["pending_version_id"],
        "last_known_good_version_id": registry["last_known_good_version_id"],
        "node_count": len(nodes),
        "nodes": [
            {
                "id": node["id"],
                "node_kind": node["node_kind"],
                "label": node["label"],
                "member_count": member_counts.get(node["id"], 0),
            }
            for node in nodes[:SUMMARY_NODE_LIMIT]
        ],
        "nodes_omitted": max(0, len(nodes) - SUMMARY_NODE_LIMIT),
        "versions": [
            {
                "id": version["id"],
                "version_number": version["version_number"],
                "status": version["status"],
                "content_hash": version["content_hash"],
                "vocabulary_version_id": version["vocabulary_version_id"],
                "origin_preset_version_id": version["origin_preset_version_id"],
                # ISO STRING, not the raw column. This payload is returned straight
                # from `GET /api/projects/{id}/capabilities/{key}/datastreams` through
                # `read_master_data_owner`, and `JSONResponse` cannot serialize a
                # `datetime`: every Project with a PUBLISHED registry version answered
                # 503 on the per-Datastream capability projection -- the read model
                # behind the activation preview. Unseen because no Project had ever
                # published one (measured 2026-08-23: 0 Country registries in
                # production). Every other timestamp on a served payload already does
                # this; this one was the exception.
                "published_at": (
                    version["published_at"].isoformat()
                    if hasattr(version["published_at"], "isoformat")
                    else version["published_at"]
                ),
            }
            for version in versions[:SUMMARY_VERSION_LIMIT]
        ],
        "versions_omitted": max(0, len(versions) - SUMMARY_VERSION_LIMIT),
    }


def assert_change_acknowledged(
    conn,
    *,
    project_id: str,
    registry_id: str,
    affected_node_ids: Sequence[str],
    acknowledged: bool,
) -> tuple[UsedByReference, ...]:
    """Show every dependent before a destructive change, or refuse it (AC8).

    A read failure raises out of :func:`fetch_used_by` and is *not* caught here:
    an unreadable dependency store blocks the change instead of authorizing it.
    """

    references = fetch_used_by(
        conn, project_id=project_id, registry_id=registry_id, node_ids=affected_node_ids
    )
    if references and not acknowledged:
        raise MasterDataConflict(
            f"{len(references)} dependent reference(s) must be reviewed before this change"
        )
    return references


# ---------------------------------------------------------------------------
# The organization half of the same lifecycle (Story 48.5).
#
# Everything above is written for a Project registry, because Country -- the
# first object to mount here -- is a Project decision. Migration 143 made the
# other scope real in the schema: `master_data_registries.scope`, a nullable
# `project_id`, node-scoped versions, SKOS-typed aliases and project
# associations. Nothing in Python could reach any of it.
#
# An organization object is the case where reuse is the point: one identity,
# recorded once, assigned a DIFFERENT role in each Project that cares about it.
# Copying it per Project is the second authority this area exists to remove, so
# the functions below read and write the same five tables at organization scope
# rather than beside them.
#
# Two rules hold throughout, and they are why these are separate functions
# rather than an `if project_id is None` branch inside each one above:
#
#   * A Project surface may read its OWN associations and nothing else. There is
#     no function here answering "which other Projects use this identity?" from
#     a Project-scoped caller -- the absence IS the confidentiality boundary,
#     and a filter added later would be one forgotten WHERE away from leaking it.
#   * A node-scoped version counts per identity, which is what migration 143's
#     two partial unique indexes already enforce; counting per registry would
#     reject the second identity's first version as a duplicate of the first
#     identity's. It does NOT restart at 1 for every node: since AI-324 the
#     counter starts above every ledger that numbers that identity, so a
#     converged object continues its legacy history rather than colliding with
#     it (`_NEXT_NODE_VERSION_NUMBER`).
# ---------------------------------------------------------------------------

ORGANIZATION_SCOPE = "organization"
PROJECT_SCOPE = "project"

#: How a version's history is counted. `registry` is 140's behaviour (Country
#: publishes as one grouping); `node` gives every identity its own lifecycle.
VERSION_SCOPE_REGISTRY = "registry"
VERSION_SCOPE_NODE = "node"

#: The claims an alias may make, in the SKOS sense. Five different facts: a
#: `close` match is explicitly NOT transitive, and `negative` is a decision that
#: must survive so a refused mapping does not resurface on every later run.
ALIAS_RELATIONS = ("exact", "close", "broader", "narrower", "related", "negative")

_ALIAS_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "node_id",
    "namespace",
    "locale",
    "raw_value",
    "normalized_value",
    "relation",
    "confidence",
    "effective_from",
    "effective_to",
    "provenance",
    "provenance_reference",
    "evidence",
    "conflict_state",
    "created_by",
    "created_at",
    "retired_at",
    "retired_by",
)

_ASSOCIATION_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "node_id",
    "node_version_id",
    "project_role",
    "applicability",
    "effective_from",
    "effective_to",
    "created_by",
    "created_at",
    "retired_at",
    "retired_by",
)

_TYPE_COLUMNS = (
    "id",
    "org_id",
    "object_kind",
    "version_number",
    "label",
    "description",
    "property_schema",
    "display_hints",
    "relationship_definitions",
    "origin",
    "origin_reference",
    "origin_fingerprint",
    "content_hash",
    "created_by",
    "created_at",
)

# `import_provenance` (migration 295, Story 68.5) is read everywhere a node
# version is: a reference-import landing stamps the act that minted the version,
# and a reader must not need a second query to name it.
_NODE_VERSION_COLUMNS = (*_VERSION_COLUMNS, "node_id", "type_version_id", "import_provenance")


def normalize_alias_value(value: str) -> str:
    """Case-folded, whitespace-collapsed. Stored so a collision is a database fact.

    Deliberately conservative: it folds case and runs of whitespace and nothing
    else. Stripping punctuation or accents here would make two genuinely
    different labels collide inside a UNIQUE index, and that index is what turns
    "one string names two identities" into a refusal rather than a race.
    """

    return " ".join(str(value or "").split()).casefold()


# --- Registries -------------------------------------------------------------


def create_org_registry(
    conn,
    *,
    org_id: str,
    object_kind: str,
    label: str,
    actor: str,
    version_scope: str = VERSION_SCOPE_NODE,
) -> dict[str, Any]:
    """Mint the single organization-wide owner for one object kind."""

    if version_scope not in (VERSION_SCOPE_REGISTRY, VERSION_SCOPE_NODE):
        raise MasterDataError(f"unknown version scope: {version_scope!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_registries
                (id, org_id, project_id, object_kind, label, scope, version_scope, created_by)
            VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING {", ".join(_REGISTRY_COLUMNS)}
            """,
            (
                _mint("mdreg"),
                _required(org_id, "org_id"),
                _kind(object_kind, "object_kind"),
                _label(label, "label"),
                ORGANIZATION_SCOPE,
                version_scope,
                _required(actor, "actor"),
            ),
        )
        row = cur.fetchone()
    if row is not None:
        return _row(row, _REGISTRY_COLUMNS, "registry")
    existing = fetch_org_registry(conn, org_id=org_id, object_kind=object_kind)
    if existing is None:  # pragma: no cover - only under a concurrent delete
        raise MasterDataConflict("registry could not be created or read back")
    return existing


def fetch_org_registry(
    conn, *, org_id: str, object_kind: str | None = None, registry_id: str | None = None
) -> dict[str, Any] | None:
    if object_kind is None and registry_id is None:
        raise MasterDataError("fetch_org_registry requires object_kind or registry_id")
    clause = "object_kind = %s" if registry_id is None else "id = %s"
    needle = (
        _kind(object_kind, "object_kind")
        if registry_id is None
        else _required(registry_id, "registry_id")
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_REGISTRY_COLUMNS)}
            FROM app.master_data_registries
            WHERE org_id = %s AND project_id IS NULL AND {clause}
            """,
            (_required(org_id, "org_id"), needle),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_REGISTRY_COLUMNS, row, strict=False))


def require_org_registry(
    conn, *, org_id: str, object_kind: str | None = None, registry_id: str | None = None
) -> dict[str, Any]:
    registry = fetch_org_registry(
        conn, org_id=org_id, object_kind=object_kind, registry_id=registry_id
    )
    if registry is None:
        raise MasterDataNotFound("registry not found in this organization")
    return registry


# --- Nodes ------------------------------------------------------------------


def create_org_node(
    conn,
    *,
    org_id: str,
    registry_id: str,
    node_kind: str,
    label: str,
    actor: str,
    node_id: str | None = None,
) -> dict[str, Any]:
    """One organization identity. Stable id; the label is a projection.

    ``node_id`` CARRIES an identity that already exists elsewhere instead of
    minting a new one. Migration 143 widened `master_data_nodes_id_check` to
    accept a `bd_`/`bcl_` id for exactly this reason, and stated why in its own
    section 6: a Business Domain has consumers pinning its id since Story 45.1,
    and converging on one authority must not mint a second identity for the same
    business object -- that is not a convergence, it is a fork with a migration
    in front of it. The database CHECK is the guard: a caller that passes an id
    of any other shape is refused by the table, not by a list retyped here.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_nodes
                (id, org_id, project_id, registry_id, node_kind, label, created_by)
            VALUES (%s, %s, NULL, %s, %s, %s, %s)
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (
                _required(node_id, "node_id") if node_id is not None else _mint("mdnode"),
                _required(org_id, "org_id"),
                _required(registry_id, "registry_id"),
                _kind(node_kind, "node_kind"),
                _label(label, "label"),
                _required(actor, "actor"),
            ),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


def list_org_nodes(
    conn, *, org_id: str, registry_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    clause = "" if include_archived else "AND archived_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_COLUMNS)}
            FROM app.master_data_nodes
            WHERE org_id = %s AND project_id IS NULL AND registry_id = %s {clause}
            ORDER BY node_kind, label, id
            """,
            (_required(org_id, "org_id"), _required(registry_id, "registry_id")),
        )
        return [dict(zip(_NODE_COLUMNS, row, strict=False)) for row in cur.fetchall()]


def fetch_org_node(conn, *, org_id: str, node_id: str) -> dict[str, Any] | None:
    """Read one identity, scoped to its organization.

    Returns None rather than raising when the id belongs to another
    organization, so the caller answers the same not-found a missing id
    produces. Existence stays hidden because the two cases are one case here.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_COLUMNS)}
            FROM app.master_data_nodes
            WHERE org_id = %s AND project_id IS NULL AND id = %s
            """,
            (_required(org_id, "org_id"), _required(node_id, "node_id")),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_NODE_COLUMNS, row, strict=False))


def rename_org_node(conn, *, org_id: str, node_id: str, label: str) -> dict[str, Any]:
    """Change what an organization identity is CALLED. Its id survives untouched.

    The organization twin of :func:`rename_node`, and it exists for the same
    reason the whole block below this comment exists: every clause of the
    Project-scoped function reads `project_id = %s`, which an organization row
    (`project_id IS NULL`) never matches -- so renaming a converged Business
    Domain silently updated zero rows and raised "node not found" for a node
    that plainly exists.

    The label is a PROJECTION of the identity, never the identity: nothing that
    pinned this id is touched, and the current version's payload is re-published
    by the caller so the two cannot disagree about the name.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_nodes
               SET label = %s, updated_at = NOW()
             WHERE org_id = %s AND project_id IS NULL AND id = %s
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (
                _label(label, "label"),
                _required(org_id, "org_id"),
                _required(node_id, "node_id"),
            ),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


def archive_org_node(conn, *, org_id: str, node_id: str) -> dict[str, Any]:
    """Retire an identity. Nothing is deleted -- older versions still name it."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_nodes
               SET archived_at = NOW(), updated_at = NOW()
             WHERE org_id = %s AND project_id IS NULL AND id = %s AND archived_at IS NULL
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (_required(org_id, "org_id"), _required(node_id, "node_id")),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


def assess_org_node_impact(conn, *, org_id: str, node_id: str) -> NodeImpact:
    """Live consumers of one ORGANIZATION identity, or an outage -- never a zero.

    The organization twin of :func:`assess_node_impact`, and it exists because
    the Project-scoped one cannot answer this question at all: an organization
    node has ``project_id IS NULL``, so ``WHERE project_id = %s AND node_id = %s``
    matches nothing and reports an identity nobody depends on. Every converged
    Business Domain and Business Classification is such a node
    (``master_data_convergence._apply`` -> ``create_org_node``), so that silent
    zero is the whole of the taxonomy.

    A consumer still lives in exactly ONE Project -- migration 143 kept
    ``master_data_used_by.project_id`` NOT NULL for that -- so the question here
    is "who depends on this identity, in any Project of this organization", and
    the organization is proved through the node rather than trusted from the
    caller.

    THIS IS THE STORE'S OWN READER, and it is where a command should get its
    used-by answer from. ``master_data_commands.assess_org_identity_impact``
    widens it with the governed Context Hub links, which is its business; the
    used-by half belongs here, so the two cannot drift into two answers to one
    question.

    Raises:
        MasterDataUnavailable: the used-by store could not be read. Same contract
            as :func:`assess_node_impact`: "I could not check" and "nothing
            depends on this" are different facts, and only one is safe to act on.
    """

    org_id = _required(org_id, "org_id")
    node_id = _required(node_id, "node_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.consumer_kind, u.consumer_id, u.consumer_label,
                       u.consumer_version_id
                  FROM app.master_data_used_by u
                  JOIN app.master_data_nodes n ON n.id = u.node_id
                 WHERE n.org_id = %s AND n.project_id IS NULL AND u.node_id = %s
                   AND u.released_at IS NULL
                 ORDER BY u.consumer_kind, u.consumer_id
                """,
                (org_id, node_id),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- any read failure is an outage here
        raise MasterDataUnavailable(
            f"the used-by store could not be read for node {node_id}: {type(exc).__name__}"
        ) from exc
    consumers = tuple(
        {
            "consumer_kind": r[0],
            "consumer_id": r[1],
            "consumer_label": r[2],
            "consumer_version_id": r[3],
        }
        for r in rows
    )
    return NodeImpact(node_id=node_id, consumers=consumers)


def archive_org_node_guarded(
    conn,
    *,
    org_id: str,
    node_id: str,
    acknowledge_impact: bool = False,
    impact: NodeImpact | None = None,
) -> dict[str, Any]:
    """:func:`archive_org_node`, refused while live consumers still name the node.

    The organization twin of :func:`archive_node`, with the same three
    properties: the impact is READ before the write, an unreadable store blocks
    rather than authorizes, and ``acknowledge_impact`` is a separate argument
    because the decision has to be made rather than inherited.

    ``impact`` lets a caller that has already assessed pass its reading in, so
    the consumers shown to a human and the consumers the guard acts on are the
    same evidence rather than two reads a moment apart.

    Raises:
        MasterDataConflict: live consumers exist and the impact was not acknowledged.
        MasterDataUnavailable: the used-by store could not be read (fail closed).
    """

    org_id = _required(org_id, "org_id")
    node_id = _required(node_id, "node_id")
    if impact is None:
        impact = assess_org_node_impact(conn, org_id=org_id, node_id=node_id)
    if not impact.is_clear and not acknowledge_impact:
        raise MasterDataConflict(
            f"node {node_id} is still used by {impact.describe()}. Archiving it "
            "would break those consumers. Release them, or confirm the archive "
            "with the impact acknowledged."
        )
    return archive_org_node(conn, org_id=org_id, node_id=node_id)


def restore_org_node(conn, *, org_id: str, node_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_nodes
               SET archived_at = NULL, updated_at = NOW()
             WHERE org_id = %s AND project_id IS NULL AND id = %s AND archived_at IS NOT NULL
            RETURNING {", ".join(_NODE_COLUMNS)}
            """,
            (_required(org_id, "org_id"), _required(node_id, "node_id")),
        )
        return _row(cur.fetchone(), _NODE_COLUMNS, "node")


# --- Node-scoped versions ---------------------------------------------------


def node_version_digest(
    *,
    node_id: str,
    payload: Mapping[str, Any],
    type_version_id: str | None,
    vocabulary_version_id: str | None,
    memberships: Sequence[Membership] = (),
) -> str:
    """The content identity of ONE identity's revision.

    `memberships` is written into the body only when there ARE edges, and the
    asymmetry with :meth:`DraftContent.digest` -- which always carries the key --
    is deliberate rather than an oversight:

    * every node version written before this function existed hashed a body with
      no `memberships` key, and a formula that always added `[]` would give the
      same content a new digest, so a stored hash would stop matching the
      content it names;
    * Story 68.5's per-entity no-op -- *"this snapshot changes nothing for this
      entity, mint nothing"* -- is decided by comparing this digest across two
      imports. The reference route writes no edges, so an unconditional key
      would move every digest once and prove a change that did not happen.

    An edge that MOVES still moves the digest, which is the property that makes
    ``publish_node_version(expected_content_hash=...)`` a real optimistic lock:
    a regroup between review and publication is a different content, and the
    confirmation must fail closed on it.
    """

    body: dict[str, Any] = {
        "node_id": _required(node_id, "node_id"),
        "payload": dict(payload),
        "type_version_id": type_version_id,
        "vocabulary_version_id": vocabulary_version_id,
    }
    if memberships:
        body["memberships"] = sorted(
            (edge.as_dict() for edge in memberships), key=canonical_json
        )
    return content_hash(body)


#: THE NEXT VERSION NUMBER OF ONE IDENTITY -- ABOVE EVERY LEDGER THAT NUMBERS IT.
#:
#: AI-324, decided by Jean on 2026-08-31: *a version number designates ONE
#: content, ever*. Until then this counter read the authority's ledger alone and
#: restarted at 1 for a converged identity, while `business_identity_catalogue`
#: unions the authority with the SUPERSEDED ledger per `(id, version_number)` --
#: so a domain carrying legacy revisions 1..3, converged and then revised, minted
#: authority v2 for content the legacy v2 does not hold, and the resolver's
#: authority-wins branch silently hid one of the two. Seeding from the union's
#: maximum removes the collision at the source: the number the authority mints is
#: one nobody has ever spent for this identity.
#:
#: DERIVED, NOT STORED. The maximum is read from the ledgers themselves at every
#: mint rather than kept in a counter column: a stored counter is a second thing
#: that can disagree with the rows it counts, and it would have to be backfilled
#: for every identity that already exists. The two legacy ledgers are IMMUTABLE
#: (migration 130's `reject_business_taxonomy_version_mutation` refuses UPDATE
#: and DELETE) and their only writer left is the org-creation seed, which runs
#: before any node exists -- so the maximum this reads cannot move underneath a
#: mint. What CAN race is a second mint on the same identity, and the `FOR UPDATE`
#: on the node row below is what serialises those.
#:
#: BOTH legacy ledgers are asked, and neither is gated on the node's kind. The id
#: spaces are disjoint by construction (`bdm_` / `bcl_`, migration 143's widened
#: `master_data_nodes_id_check`), so an identity that is not a business domain
#: matches nothing and the GREATEST falls through to the authority's own maximum.
#: Their RLS policies are the org-membership one the authority half already
#: carries (migration 273), so this reading is visible exactly where the
#: authority's is -- it opens no row a caller could not already see.
_NEXT_NODE_VERSION_NUMBER = """
    SELECT GREATEST(
        COALESCE((SELECT MAX(version_number)
                    FROM app.master_data_object_versions
                   WHERE node_id = %s), 0),
        COALESCE((SELECT MAX(version_number)
                    FROM app.mdm_business_domain_versions
                   WHERE domain_id = %s), 0),
        COALESCE((SELECT MAX(version_number)
                    FROM app.mdm_business_classification_versions
                   WHERE classification_id = %s), 0)
    ) + 1
"""


def create_node_version(
    conn,
    *,
    org_id: str,
    registry_id: str,
    node_id: str,
    payload: Mapping[str, Any],
    actor: str,
    type_version_id: str | None = None,
    vocabulary_version_id: str | None = None,
    effective_date: date | str | None = None,
    import_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Open a draft revision of ONE identity.

    THE NUMBER IT MINTS IS ABOVE EVERY LEDGER THAT NUMBERS THIS IDENTITY, never
    just the authority's own -- see `_NEXT_NODE_VERSION_NUMBER` for why (AI-324).

    `import_provenance` (Story 68.5) stamps the version with the import that
    landed it -- execution, pinned mapping, ledger row. It is deliberately NOT
    part of the content_hash: the digest is the identity of the CONTENT, and
    the same attributes carried by a new execution are the same content. That
    separation is what makes the reference route's per-entity no-op provable.

    THE VERSION SITS IN THE SCOPE ITS NODE SITS IN, AND IT IS READ FROM THE NODE
    (AI-328). `project_id` used to be written as a literal NULL here, which is
    the truth for an organization identity and a falsehood for every other one:
    a Product and an Activity are PROJECT nodes (`entity_reference_import`,
    `observed_entities`), and a version filed under no project was severed from
    the node it belongs to in three ways at once --

    * `fk_master_data_versions_node (project_id, node_id)` is MATCH SIMPLE, so a
      NULL in the first column meant the reference was never checked at all;
    * `uq_master_data_versions_current_node (project_id, node_id)` is a unique
      index over a NULL, and NULLs are distinct, so "exactly one current
      revision per identity" guaranteed nothing for those rows;
    * `governance_read_model._CLIENT_OBJECT_INSTANCES` joins the revision on
      `v.project_id = n.project_id`, so the Overview reported every Product as
      having no version and no base -- and the rename lock of 2026-08-30, which
      is preconditioned on exactly that base, could not fire.

    Taking the scope from the NODE rather than from a parameter is what makes it
    unforgeable: there is one answer to "which project is this identity in", and
    no caller can hold a different one. The read is folded into the lock below,
    so it costs no extra round trip, and a node that does not exist is now said
    rather than silently minting a version that references nothing.
    """

    org_id = _required(org_id, "org_id")
    digest = node_version_digest(
        node_id=node_id,
        payload=payload,
        type_version_id=type_version_id,
        vocabulary_version_id=vocabulary_version_id,
    )
    with conn.cursor() as cur:
        # THE LOCK THAT MAKES THE COUNTER A COUNTER. Two mints on one identity
        # read the same maximum without it and both claim the same number; the
        # unique index then refuses the loser with a duplicate-key error instead
        # of giving it the next number. The NODE row is the thing locked because
        # it is the one row that exists for every mint -- the first version of a
        # converged identity has no revision to lock.
        cur.execute(
            "SELECT project_id FROM app.master_data_nodes WHERE id = %s AND org_id = %s "
            "FOR UPDATE",
            (node_id, org_id),
        )
        scope = cur.fetchone()
        if scope is None:
            raise MasterDataNotFound(
                "no such identity in this organization: a version belongs to a node"
            )
        (node_project_id,) = scope
        cur.execute(_NEXT_NODE_VERSION_NUMBER, (node_id, node_id, node_id))
        (next_number,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO app.master_data_object_versions
                (id, org_id, project_id, registry_id, node_id, version_number, status,
                 vocabulary_version_id, type_version_id, payload, content_hash,
                 effective_date, import_provenance, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s::jsonb, %s, %s,
                    %s::jsonb, %s)
            RETURNING {", ".join(_NODE_VERSION_COLUMNS)}
            """,
            (
                _mint("mdver"),
                org_id,
                node_project_id,
                _required(registry_id, "registry_id"),
                node_id,
                next_number,
                vocabulary_version_id,
                type_version_id,
                json.dumps(dict(payload)),
                digest,
                _as_date(effective_date, "effective_date"),
                json.dumps(dict(import_provenance)) if import_provenance is not None else None,
                _required(actor, "actor"),
            ),
        )
        return _row(cur.fetchone(), _NODE_VERSION_COLUMNS, "version")


def fetch_node_version(conn, *, org_id: str, version_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_VERSION_COLUMNS)}
            FROM app.master_data_object_versions
            WHERE org_id = %s AND id = %s
            """,
            (_required(org_id, "org_id"), _required(version_id, "version_id")),
        )
        row = cur.fetchone()
    return None if row is None else dict(zip(_NODE_VERSION_COLUMNS, row, strict=False))


def list_node_versions(conn, *, org_id: str, node_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_VERSION_COLUMNS)}
            FROM app.master_data_object_versions
            WHERE org_id = %s AND node_id = %s
            ORDER BY version_number DESC
            """,
            (_required(org_id, "org_id"), _required(node_id, "node_id")),
        )
        return [
            dict(zip(_NODE_VERSION_COLUMNS, row, strict=False)) for row in cur.fetchall()
        ]


def publish_node_version(
    conn,
    *,
    org_id: str,
    version_id: str,
    actor: str,
    expected_content_hash: str | None = None,
) -> dict[str, Any]:
    """Make one identity's revision current, atomically, or change nothing.

    The registry pointers are deliberately NOT moved. Under node scope they
    would name whichever identity happened to publish last, which is a lie about
    what the registry currently is. Each identity's own `current` row is the
    pointer, and 143's partial unique index guarantees there is exactly one.
    """

    version = fetch_node_version(conn, org_id=org_id, version_id=version_id)
    if version is None:
        raise MasterDataNotFound("version not found in this organization")
    if not version.get("node_id"):
        raise MasterDataError("publish_node_version expects a node-scoped version")
    if version["status"] not in ("draft", "candidate"):
        raise MasterDataConflict("only a draft or candidate revision can be published")
    if expected_content_hash and expected_content_hash != version["content_hash"]:
        raise MasterDataConflict(
            "the revision changed since it was reviewed; re-prepare before publishing"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.master_data_object_versions
               SET status = 'superseded'
             WHERE node_id = %s AND status = 'current' AND id <> %s
            """,
            (version["node_id"], version_id),
        )
        cur.execute(
            f"""
            UPDATE app.master_data_object_versions
               SET status = 'current', published_at = NOW(), published_by = %s
             WHERE org_id = %s AND id = %s
            RETURNING {", ".join(_NODE_VERSION_COLUMNS)}
            """,
            (_required(actor, "actor"), org_id, version_id),
        )
        return _row(cur.fetchone(), _NODE_VERSION_COLUMNS, "version")


def _org_node_ids(conn, *, org_id: str) -> list[str]:
    """Every identity this organization owns, across registries."""

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.master_data_nodes "
            "WHERE org_id = %s AND project_id IS NULL",
            (_required(org_id, "org_id"),),
        )
        return [str(row[0]) for row in cur.fetchall()]


def fetch_org_memberships(conn, *, version_id: str) -> tuple[Membership, ...]:
    """The edges one organization-scoped version carries."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT parent_node_id, child_node_id, child_value,
                   effective_from, effective_to, display_order
            FROM app.master_data_memberships
            WHERE version_id = %s AND project_id IS NULL
            ORDER BY display_order, parent_node_id, child_node_id, child_value
            """,
            (_required(version_id, "version_id"),),
        )
        return tuple(
            Membership(
                parent_node_id=row[0],
                child_node_id=row[1],
                child_value=row[2],
                effective_from=row[3],
                effective_to=row[4],
                display_order=row[5],
            )
            for row in cur.fetchall()
        )


def replace_org_draft_memberships(
    conn, *, org_id: str, version_id: str, memberships: Sequence[Membership]
) -> dict[str, Any]:
    """Rewrite an organization draft's edges wholesale and re-derive its digest.

    THE GAP THIS CLOSES. The organization layer of this module was added with a
    node writer, a version writer, an alias writer and an association writer --
    and no edge writer. `replace_draft_memberships` above cannot serve it: every
    one of its clauses reads `project_id = %s`, and an organization row carries
    `project_id IS NULL`, which that comparison never matches. So an
    organization-scoped hierarchy was expressible in the schema (migration 143
    made `master_data_memberships.project_id` nullable and added the
    single-column foreign keys that keep the edge enforced at any scope) and
    unreachable from Python. A Business Domain tree had nowhere to live.

    THE EDGE BELONGS TO THE CHILD'S VERSION, not the parent's. Under node scope
    the version IS the identity's own history, and moving an object is a change
    to the object that moved -- so publishing the child is what commits the move,
    and one regroup mints one version rather than one per branch touched. Under
    registry scope (Country) the whole grouping publishes at once and the edges
    hang off the registry version; both shapes use this table, which is what
    migration 143's `version_scope` decides.
    """

    version = fetch_node_version(conn, org_id=org_id, version_id=version_id)
    if version is None:
        raise MasterDataNotFound("version not found in this organization")
    if version["status"] != "draft":
        raise MasterDataConflict("only a draft revision can be edited")
    if version.get("project_id") is not None:
        raise MasterDataError(
            "replace_org_draft_memberships expects an organization-scoped version"
        )

    edges = tuple(memberships)
    # Node resolution is ORGANIZATION-wide, not registry-wide, and that is the
    # difference between this function and its Project-scoped sibling. A
    # Business Domain and the classifications under it are two object kinds, so
    # they are two registries -- and "this classification sits under that
    # domain" is an edge ACROSS them. Restricting the known set to one registry
    # would reject the very relation the Master Data hierarchy exists to carry,
    # with the message "unknown parent node" for a node that plainly exists. The
    # database agrees: migration 143's single-column foreign keys reference
    # `master_data_nodes(id)`, with no registry in them.
    nodes = _org_node_ids(conn, org_id=org_id)
    # A version that pins no vocabulary has no controlled value set to check
    # against -- a legal object kind (a pure node hierarchy), not a skipped
    # check. The Business Domain tree is exactly that case.
    values: set[str] | None = None
    if version["vocabulary_version_id"]:
        vocabulary = fetch_vocabulary_version(
            conn, vocabulary_version_id=version["vocabulary_version_id"]
        )
        if vocabulary is None:
            raise MasterDataUnavailable("the pinned vocabulary version is unreadable")
        values = {str(entry.get("code")) for entry in vocabulary["entries"]}
    outcome = validate_hierarchy(edges, known_node_ids=nodes, known_values=values)
    if not outcome.ok:
        raise MasterDataConflict("; ".join(outcome.errors))

    digest = node_version_digest(
        node_id=str(version["node_id"]),
        payload=version["payload"] or {},
        type_version_id=version.get("type_version_id"),
        vocabulary_version_id=version["vocabulary_version_id"],
        memberships=edges,
    )
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.master_data_memberships "
            "WHERE version_id = %s AND project_id IS NULL",
            (version_id,),
        )
        for edge in edges:
            cur.execute(
                """
                INSERT INTO app.master_data_memberships
                    (id, project_id, version_id, parent_node_id, child_node_id,
                     child_value, effective_from, effective_to, display_order)
                VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _mint("mdmem"),
                    version_id,
                    edge.parent_node_id,
                    edge.child_node_id,
                    edge.child_value,
                    edge.effective_from,
                    edge.effective_to,
                    edge.display_order,
                ),
            )
        cur.execute(
            f"""
            UPDATE app.master_data_object_versions
               SET content_hash = %s
             WHERE org_id = %s AND id = %s
            RETURNING {", ".join(_NODE_VERSION_COLUMNS)}
            """,
            (digest, org_id, version_id),
        )
        return _row(cur.fetchone(), _NODE_VERSION_COLUMNS, "version")


def current_node_versions(
    conn, *, org_id: str, registry_id: str, node_ids: Sequence[str] | None = None
) -> dict[str, dict[str, Any]]:
    """The current published revision of each identity, keyed by node id."""

    clause = ""
    params: list[Any] = [_required(org_id, "org_id"), _required(registry_id, "registry_id")]
    if node_ids is not None:
        if not node_ids:
            return {}
        clause = "AND node_id = ANY(%s)"
        params.append(list(node_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_NODE_VERSION_COLUMNS)}
            FROM app.master_data_object_versions
            WHERE org_id = %s AND registry_id = %s AND status = 'current'
              AND node_id IS NOT NULL {clause}
            """,
            tuple(params),
        )
        rows = [
            dict(zip(_NODE_VERSION_COLUMNS, row, strict=False)) for row in cur.fetchall()
        ]
    return {str(row["node_id"]): row for row in rows}


def current_node_version_id(
    conn, *, org_id: str, node_id: str, for_update: bool = False
) -> str | None:
    """The IDENTITY of the revision this object is at right now, or None.

    THIS IS THE OPTIMISTIC LOCK'S BASE, AND IT IS AN ID RATHER THAN A NUMBER ON
    PURPOSE, AND AI-324 DID NOT CHANGE THAT. Two ledgers still hold revisions of
    the same identity -- the authority and the superseded store -- and
    `business_identity_catalogue` still unions them so a convergence cannot lose
    a revision. What AI-324 fixed is that the numbers no longer COLLIDE: the
    authority mints above the union's maximum (`_NEXT_NODE_VERSION_NUMBER`), so a
    number designates one content. That makes the number honest to SHOW; it still
    does not make it a base to be HELD TO. A version row's `id` is minted once,
    never reused and never renumbered, and it survives a renumbering of the
    counter beside it: two readers holding the same id saw the same object, and a
    reader holding an id the object has left is stale by construction. The rename
    lock is therefore untouched by the renumbering, by construction and not by
    luck.

    `for_update` takes the row lock. It is the ONLY reading that can decide a
    race: two renames that both read this without the lock both see the same
    base and both proceed. Under `FOR UPDATE` the second waits, and after the
    first commits Postgres re-checks `status = 'current'` against the updated
    row -- which no longer matches -- so the loser reads None or the winner's new
    version, and either way it does not read what it stated.

    Returns None where the object has no PUBLISHED revision at all -- an
    identity in a registry-scoped registry (Country) keeps its history one level
    up, and a node-scoped identity nobody has revised yet has none. Both are
    legitimate bases to have read, and a caller states them as such. What that
    absence must NOT do is make the lock unfireable for the whole life of the
    object, which is why a rename on a node-scoped identity mints its first
    revision rather than passing through (`master_data_commands._republish`).
    """

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.master_data_object_versions "
            "WHERE org_id = %s AND node_id = %s AND status = 'current'"
            + (" FOR UPDATE" if for_update else ""),
            (_required(org_id, "org_id"), _required(node_id, "node_id")),
        )
        row = cur.fetchone()
    return None if row is None else str(row[0])


def node_versions_per_identity(conn, *, node_id: str) -> bool:
    """Does THIS identity keep a history of its own, or its grouping's?

    The answer is `master_data_registries.version_scope` and nothing else --
    migration 143's own column, the same one `governance_read_model` reads to
    decide which version count an object reports. Asking the registry rather
    than the node's KIND is what keeps this from being a guess: 143 forbids
    guessing which object kind is a Product, and a list of kinds here would be
    that guess wearing an `IN` clause. A Project declares its collections and
    says how they version; this reads what it said.

    `False` for an identity in a registry-scoped registry (Country: the whole
    grouping publishes as one act, so minting a node version would open a second
    history beside it) and for a node that does not exist.
    """

    with conn.cursor() as cur:
        cur.execute(
            "SELECT registry.version_scope "
            "FROM app.master_data_nodes node "
            "JOIN app.master_data_registries registry ON registry.id = node.registry_id "
            "WHERE node.id = %s",
            (_required(node_id, "node_id"),),
        )
        row = cur.fetchone()
    return row is not None and str(row[0]) == VERSION_SCOPE_NODE


# --- Aliases ----------------------------------------------------------------


def record_alias(
    conn,
    *,
    org_id: str,
    node_id: str,
    namespace: str,
    raw_value: str,
    relation: str,
    actor: str,
    locale: str | None = None,
    confidence: float | None = None,
    provenance: str = "operator",
    provenance_reference: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one typed claim about a string.

    A unique violation on the live-exact index is not an error to swallow: it
    means the same string already names a different identity in the same
    namespace, and the caller must surface that collision rather than pick.
    """

    if relation not in ALIAS_RELATIONS:
        raise MasterDataError(f"unknown alias relation: {relation!r}")
    normalized = normalize_alias_value(raw_value)
    if not normalized:
        raise MasterDataError("alias value is empty")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_aliases
                (id, org_id, project_id, node_id, namespace, locale, raw_value,
                 normalized_value, relation, confidence, provenance,
                 provenance_reference, evidence, created_by)
            VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING {", ".join(_ALIAS_COLUMNS)}
            """,
            (
                _mint("mdali"),
                _required(org_id, "org_id"),
                _required(node_id, "node_id"),
                _required(namespace, "namespace"),
                locale,
                str(raw_value).strip(),
                normalized,
                relation,
                confidence,
                provenance,
                provenance_reference,
                json.dumps(dict(evidence or {})),
                _required(actor, "actor"),
            ),
        )
        return _row(cur.fetchone(), _ALIAS_COLUMNS, "alias")


def retire_alias(conn, *, org_id: str, alias_id: str, actor: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_aliases
               SET retired_at = NOW(), retired_by = %s
             WHERE org_id = %s AND id = %s AND retired_at IS NULL
            RETURNING {", ".join(_ALIAS_COLUMNS)}
            """,
            (
                _required(actor, "actor"),
                _required(org_id, "org_id"),
                _required(alias_id, "alias_id"),
            ),
        )
        return _row(cur.fetchone(), _ALIAS_COLUMNS, "alias")


def list_aliases(
    conn,
    *,
    org_id: str,
    node_ids: Sequence[str] | None = None,
    include_retired: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["org_id = %s"]
    params: list[Any] = [_required(org_id, "org_id")]
    if node_ids is not None:
        if not node_ids:
            return []
        clauses.append("node_id = ANY(%s)")
        params.append(list(node_ids))
    if not include_retired:
        clauses.append("retired_at IS NULL")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_ALIAS_COLUMNS)}
            FROM app.master_data_aliases
            WHERE {" AND ".join(clauses)}
            ORDER BY node_id, relation, normalized_value
            """,
            tuple(params),
        )
        return [dict(zip(_ALIAS_COLUMNS, row, strict=False)) for row in cur.fetchall()]


# --- Project associations ---------------------------------------------------


def set_project_association(
    conn,
    *,
    org_id: str,
    project_id: str,
    node_id: str,
    project_role: str,
    actor: str,
    node_version_id: str | None = None,
    applicability: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assign an organization identity a role in ONE Project.

    Idempotent through the live-association index: re-assigning the same role
    returns the existing row instead of minting a rival one.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_project_associations
                (id, org_id, project_id, node_id, node_version_id, project_role,
                 applicability, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT DO NOTHING
            RETURNING {", ".join(_ASSOCIATION_COLUMNS)}
            """,
            (
                _mint("mdass"),
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
                _required(node_id, "node_id"),
                node_version_id,
                _kind(project_role, "project_role"),
                json.dumps(dict(applicability or {})),
                _required(actor, "actor"),
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return _row(row, _ASSOCIATION_COLUMNS, "association")
        cur.execute(
            f"""
            SELECT {", ".join(_ASSOCIATION_COLUMNS)}
            FROM app.master_data_project_associations
            WHERE project_id = %s AND node_id = %s AND project_role = %s
              AND retired_at IS NULL AND effective_to IS NULL
            """,
            (project_id, node_id, project_role),
        )
        return _row(cur.fetchone(), _ASSOCIATION_COLUMNS, "association")


def retire_project_association(
    conn, *, project_id: str, association_id: str, actor: str
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.master_data_project_associations
               SET retired_at = NOW(), retired_by = %s
             WHERE project_id = %s AND id = %s AND retired_at IS NULL
            RETURNING {", ".join(_ASSOCIATION_COLUMNS)}
            """,
            (
                _required(actor, "actor"),
                _required(project_id, "project_id"),
                _required(association_id, "association_id"),
            ),
        )
        return _row(cur.fetchone(), _ASSOCIATION_COLUMNS, "association")


def list_project_associations(
    conn, *, project_id: str, node_ids: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """The associations of ONE Project.

    There is deliberately no sibling-Project variant of this function. A Project
    surface must not be able to learn that another Project tracks an identity --
    not by a count, not by an id it cannot resolve, not by a slower response.
    The way to guarantee that is for the query never to exist.
    """

    clauses = ["project_id = %s", "retired_at IS NULL", "effective_to IS NULL"]
    params: list[Any] = [_required(project_id, "project_id")]
    if node_ids is not None:
        if not node_ids:
            return []
        clauses.append("node_id = ANY(%s)")
        params.append(list(node_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_ASSOCIATION_COLUMNS)}
            FROM app.master_data_project_associations
            WHERE {" AND ".join(clauses)}
            ORDER BY node_id, project_role
            """,
            tuple(params),
        )
        return [dict(zip(_ASSOCIATION_COLUMNS, row, strict=False)) for row in cur.fetchall()]


# --- Object types -----------------------------------------------------------


def ensure_type_version(
    conn,
    *,
    org_id: str | None,
    object_kind: str,
    label: str,
    property_schema: Mapping[str, Any],
    actor: str,
    description: str = "",
    display_hints: Mapping[str, Any] | None = None,
    relationship_definitions: Sequence[Mapping[str, Any]] | None = None,
    origin: str = "client",
    origin_reference: str | None = None,
) -> dict[str, Any]:
    """Content-address one object type. The same definition resolves to one row."""

    import jsonschema  # noqa: PLC0415 -- already a dependency; imported at call time

    jsonschema.Draft202012Validator.check_schema(dict(property_schema))
    body = {
        "object_kind": _kind(object_kind, "object_kind"),
        "label": _label(label, "label"),
        "description": description,
        "property_schema": dict(property_schema),
        "display_hints": dict(display_hints or {}),
        "relationship_definitions": [dict(item) for item in (relationship_definitions or [])],
    }
    digest = content_hash(body)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_TYPE_COLUMNS)}
            FROM app.master_data_type_versions
            WHERE COALESCE(org_id, '') = COALESCE(%s, '')
              AND object_kind = %s AND content_hash = %s
            """,
            (org_id, body["object_kind"], digest),
        )
        row = cur.fetchone()
        if row is not None:
            return dict(zip(_TYPE_COLUMNS, row, strict=False))
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.master_data_type_versions
            WHERE COALESCE(org_id, '') = COALESCE(%s, '') AND object_kind = %s
            """,
            (org_id, body["object_kind"]),
        )
        (next_number,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO app.master_data_type_versions
                (id, org_id, object_kind, version_number, label, description,
                 property_schema, display_hints, relationship_definitions,
                 origin, origin_reference, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
            RETURNING {", ".join(_TYPE_COLUMNS)}
            """,
            (
                _mint("mdtyp"),
                org_id,
                body["object_kind"],
                next_number,
                body["label"],
                body["description"],
                json.dumps(body["property_schema"]),
                json.dumps(body["display_hints"]),
                json.dumps(body["relationship_definitions"]),
                origin,
                origin_reference,
                digest,
                _required(actor, "actor"),
            ),
        )
        return _row(cur.fetchone(), _TYPE_COLUMNS, "type version")
