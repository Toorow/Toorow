"""The business taxonomy converges into the Master Data authority (Story 49.2, AC1).

WHAT WAS TRUE BEFORE THIS MODULE, MEASURED 2026-08-25
------------------------------------------------------
Two stores answered under one section of the console::

    governance_read_model.py:446   classifications lens -> app.mdm_business_classifications
    governance_read_model.py:665   registries lens      -> app.master_data_registries

and the older of the two destroyed its rows (``business_taxonomy.py:1122``,
``DELETE FROM app.mdm_business_links``). AC1 asks for "one extensible Master
Data authority" and names the condition it is met under: *"superseded Context
and tracked-entity mutation paths cease to be writers"*. Migration 143 built the
receiving schema on 2026-07-30 and moved nothing, saying so in its own words --
and that was right, for the reason it gave.

WHY THIS IS A COMMAND AND NOT A MIGRATION
-----------------------------------------
The Implementation Gate forbids guessing which classification is a Product and
which is an Activity. It does **not** forbid moving the rows: its item 4 states
the mapping itself -- *"migrate ambiguous business-classification rows as
classifications, never guess that they are Products or Activities"*. So nothing
below interprets anything. A domain converges as a domain, a classification as a
classification, and ``classification_type`` is CARRIED in the version payload as
the organization wrote it.

What still cannot be a migration is the ACT. Converging an organization's
taxonomy is a decision with an actor, a reason, an audit row and a way to be
replayed -- the same shape every other governed act in this repository has -- and
a migration answers to nobody, runs once, and cannot be refused. So the rows move
through :func:`converge_org_taxonomy`, a durable operation
(``core.operations.execute_operation``, AD-27): mutation, audit row and outbox
event commit in ONE transaction, and a replayed idempotency key returns the first
result instead of acting twice.

THE FOUR PROPERTIES THAT MAKE IT SAFE TO RUN TWICE
--------------------------------------------------
1. **Idempotent below the idempotency key too.** A second call with a NEW key is
   not an error and not a duplicate: it re-reads what is left to converge and
   finds nothing, reporting ``converged=0, already_converged=N``. Relying on the
   key alone would make a retry after a client timeout mint a second identity.
2. **Identity-preserving.** The node carries the taxonomy row's OWN id -- 143
   widened ``master_data_nodes_id_check`` to accept ``bd_``/``bcl_`` for exactly
   this -- so every consumer that pinned a Business Domain keeps resolving. A
   convergence that minted new ids would be a fork, not a convergence.
3. **Non-destructive.** The source row stays, and names the node that now holds
   its authority (``superseded_by_node_id``) and the operation that decided it
   (``superseded_by_operation_id``, migration 306). Nothing that read the old
   store yesterday stops being explainable today.
4. **Resumable.** A partially converged organization -- a node that exists while
   its source row is not yet stamped -- is completed rather than duplicated. The
   node's existence is the fact that is checked, never the stamp alone.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
* It does not merge or regroup. ``master_data_commands.SUPPORTED_ACTIONS`` says
  in writing why those two need a diff and a preview before a confirmation, and
  this story is not where that decision is taken.
* It does not retire ``business_taxonomy`` as a writer. That is the second half
  of AC1 and it is a scheduling decision, submitted for ratification in
  ``docs/product-architecture/governance.md`` (amendment of 2026-08-25) rather
  than taken here: an organization that has not converged still needs its
  taxonomy to work.
* It does not move ``app.mdm_business_links``. A link is a Context Hub relation
  between a governed object and a consumer, not an identity; Story 49.2's own
  Current Code Intelligence says "preserve useful paths as projections; do not
  make it the generic relation store". What this story does to the links is
  stop them being destroyed -- see ``business_taxonomy.retire_link``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.master_data import (
    VERSION_SCOPE_NODE,
    MasterDataConflict,
    MasterDataError,
    Membership,
    archive_org_node,
    content_hash,
    create_node_version,
    create_org_node,
    create_org_registry,
    ensure_type_version,
    fetch_org_node,
    fetch_org_registry,
    publish_node_version,
    replace_org_draft_memberships,
)
from core.operations import MutationResult, OperationSpec, execute_operation

logger = logging.getLogger(__name__)

CONVERGENCE_COMMAND = "governance.master_data.converge_business_taxonomy"

#: The two object kinds, and they are two REGISTRIES rather than one. The
#: Governance information architecture lists `business-domains` and
#: `classifications` as separate lenses, and `_load_object` resolves a Level 3
#: object by the lens it belongs to -- so folding both kinds into one registry
#: would make one of the two lenses unable to open its own objects.
DOMAIN_KIND = "business_domain"
CLASSIFICATION_KIND = "business_classification"

DOMAIN_REGISTRY_LABEL = "Business Domains"
CLASSIFICATION_REGISTRY_LABEL = "Business Classifications"

#: JSON Schema Draft 2020-12. It describes the taxonomy row as it was WRITTEN,
#: which is why `classification_type` is a free string rather than an enum: the
#: organization chose those words, and turning them into a closed list here
#: would be the interpretation the Implementation Gate forbids, performed by a
#: schema instead of by a person.
DOMAIN_PROPERTY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["slug", "name", "lifecycle_state"],
    "properties": {
        "slug": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "owner": {"type": ["string", "null"]},
        "lifecycle_state": {"type": "string", "enum": ["active", "archived"]},
    },
    "additionalProperties": True,
}

CLASSIFICATION_PROPERTY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["slug", "name", "lifecycle_state", "classification_type"],
    "properties": {
        "slug": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "owner": {"type": ["string", "null"]},
        "lifecycle_state": {"type": "string", "enum": ["active", "archived"]},
        # Carried, never interpreted. See the module docstring.
        "classification_type": {"type": "string", "minLength": 1},
        "domain_node_id": {"type": "string", "minLength": 1},
        "parent_node_id": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}

DOMAIN_RELATIONSHIPS: list[dict[str, Any]] = [
    {
        "key": "contains",
        "label": "Contains",
        "target_kind": CLASSIFICATION_KIND,
        "direction": "outbound",
        "cardinality": "one_to_many",
        "hierarchy": "tree",
        # A Business Domain tree organizes identities; it makes no claim that
        # anything sums along it. AC4 turns on that difference, and Story 49.3
        # owns metric additivity.
        "aggregation": "navigation_only",
        "effective_dated": True,
    }
]

CLASSIFICATION_RELATIONSHIPS: list[dict[str, Any]] = [
    {
        "key": "contained_by",
        "label": "Contained by",
        "target_kind": None,
        "direction": "inbound",
        "cardinality": "many_to_one",
        "hierarchy": "tree",
        "aggregation": "navigation_only",
        "effective_dated": True,
    }
]


class ConvergenceRefused(Exception):
    """The convergence was refused before any write. The caller sees 409."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": "master_data_convergence_refused",
            "message": str(self),
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Reading what would move. A plan is taken BEFORE the operation, so a refusal
# leaves no operation row behind and the acknowledged path records exactly what
# was acknowledged rather than re-reading it after the write.
# ---------------------------------------------------------------------------

_DOMAIN_FIELDS = (
    "id",
    "slug",
    "name",
    "description",
    "owner",
    "status",
    "superseded_at",
    "superseded_by_node_id",
)
_CLASSIFICATION_FIELDS = (
    "id",
    "domain_id",
    "parent_id",
    "classification_type",
    "slug",
    "name",
    "description",
    "owner",
    "status",
    "superseded_at",
    "superseded_by_node_id",
)


@dataclass(frozen=True, slots=True)
class ConvergencePlan:
    """What one organization's convergence would move, before it moves."""

    org_id: str
    domains: tuple[dict[str, Any], ...] = ()
    classifications: tuple[dict[str, Any], ...] = ()
    already_converged_domains: int = 0
    already_converged_classifications: int = 0
    unreachable_parents: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.domains and not self.classifications

    def as_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "pending_domains": len(self.domains),
            "pending_classifications": len(self.classifications),
            "already_converged_domains": self.already_converged_domains,
            "already_converged_classifications": self.already_converged_classifications,
            "unreachable_parents": list(self.unreachable_parents),
        }

    def describe(self) -> str:
        if self.is_empty:
            return (
                f"{self.already_converged_domains} business domains and "
                f"{self.already_converged_classifications} classifications are already "
                "governed by Master Data; nothing is left to converge"
            )
        return (
            f"{len(self.domains)} business domains and "
            f"{len(self.classifications)} classifications would be converged"
        )


def plan_convergence(conn, *, org_id: str) -> ConvergencePlan:
    """Read what is left to converge, in the order it must be written.

    Archived rows are converged too, and their node is archived. Skipping them
    would silently narrow the authority to the live half of the taxonomy, and a
    reader opening a Business Domain that was archived last year would find no
    governed object at all -- which reads as "it never existed".
    """

    if not isinstance(org_id, str) or not org_id.strip():
        raise MasterDataError("org_id is required")
    org_id = org_id.strip()

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_DOMAIN_FIELDS)} FROM app.mdm_business_domains "
            "WHERE org_id = %s ORDER BY created_at, id",
            (org_id,),
        )
        domain_rows = [
            dict(zip(_DOMAIN_FIELDS, row, strict=False)) for row in cur.fetchall()
        ]
        cur.execute(
            f"SELECT {', '.join(_CLASSIFICATION_FIELDS)} FROM app.mdm_business_classifications "
            "WHERE org_id = %s ORDER BY created_at, id",
            (org_id,),
        )
        classification_rows = [
            dict(zip(_CLASSIFICATION_FIELDS, row, strict=False)) for row in cur.fetchall()
        ]

    pending_domains = [row for row in domain_rows if row["superseded_at"] is None]
    pending_classifications = [
        row for row in classification_rows if row["superseded_at"] is None
    ]
    ordered, unreachable = _parents_before_children(pending_classifications, classification_rows)

    return ConvergencePlan(
        org_id=org_id,
        domains=tuple(pending_domains),
        classifications=tuple(ordered),
        already_converged_domains=len(domain_rows) - len(pending_domains),
        already_converged_classifications=(
            len(classification_rows) - len(pending_classifications)
        ),
        unreachable_parents=tuple(unreachable),
    )


def _parents_before_children(
    pending: list[dict[str, Any]], every_row: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Order classifications so a parent is always written before its child.

    A child whose parent is neither already converged nor in this batch is
    reported rather than written. It cannot happen through the product's own
    writers -- `mdm_business_classifications` has a foreign key on `parent_id`
    -- and reporting it is what tells the difference between "the taxonomy is
    inconsistent" and "the convergence silently dropped a branch".
    """

    by_id = {str(row["id"]): row for row in every_row}
    pending_ids = {str(row["id"]) for row in pending}
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    unreachable: list[str] = []

    def place(row: dict[str, Any], seen: frozenset[str]) -> bool:
        row_id = str(row["id"])
        if row_id in placed:
            return True
        if row_id in seen:  # a cycle the source trigger should have refused
            unreachable.append(row_id)
            return False
        parent_id = row["parent_id"]
        if parent_id is not None:
            parent = by_id.get(str(parent_id))
            if parent is None:
                unreachable.append(row_id)
                return False
            if str(parent_id) in pending_ids and not place(parent, seen | {row_id}):
                unreachable.append(row_id)
                return False
        ordered.append(row)
        placed.add(row_id)
        return True

    for row in pending:
        place(row, frozenset())
    return ordered, unreachable


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


def converge_org_taxonomy(
    conn,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    idempotency_key: str,
    reason: str = "",
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Move one organization's business taxonomy into the Master Data authority.

    ``project_id`` is the authorized Project the command was reached through. It
    roots the organization -- AC11: *"these routes resolve the organization from
    the authorized Project. They never trust client owner/scope/impact fields"*
    -- and is otherwise not written: the objects produced are organization-owned
    and reused by Projects through associations, never copied into one.

    Raises:
        ConvergenceRefused: the taxonomy names a parent this command cannot
            reach, so a branch would be dropped in silence.
    """

    plan = plan_convergence(conn, org_id=org_id)
    if plan.unreachable_parents:
        raise ConvergenceRefused(
            "these classifications name a parent that is neither converged nor in "
            "this batch, so converging them would drop their branch: "
            + ", ".join(plan.unreachable_parents),
            detail={"unreachable_parents": list(plan.unreachable_parents)},
        )

    def mutation(mutation_conn, operation_id: str) -> MutationResult:
        outcome = _apply(
            mutation_conn,
            org_id=org_id,
            plan=plan,
            actor=actor,
            operation_id=operation_id,
        )
        return MutationResult(
            outcome="succeeded",
            # The identities the taxonomy still owned when the plan was taken,
            # and the identities the authority owns after. Two different sets
            # only when something actually moved -- a replay hashes the same
            # empty batch twice, which is what a no-op should look like.
            before_hash=content_hash(
                {
                    "pending_domains": [str(row["id"]) for row in plan.domains],
                    "pending_classifications": [
                        str(row["id"]) for row in plan.classifications
                    ],
                }
            ),
            after_hash=content_hash(
                {
                    "converged_domains": outcome["converged_domains"],
                    "converged_classifications": outcome["converged_classifications"],
                    "domain_registry_id": outcome["domain_registry_id"],
                    "classification_registry_id": outcome["classification_registry_id"],
                }
            ),
            result={
                "org_id": org_id,
                "plan": plan.as_dict(),
                **outcome,
            },
            outbox_payload={
                "org_id": org_id,
                "project_id": project_id,
                "converged_domains": outcome["converged_domains"],
                "converged_classifications": outcome["converged_classifications"],
            },
        )

    spec = OperationSpec(
        command_type=CONVERGENCE_COMMAND,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            "governance:master-data",
            "business-taxonomy",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "master-data-convergence-v1",
            "catalog": "master-data-v1",
            "tool": "master-data-v1",
        },
        # WHAT WAS ASKED, never what the world happened to hold when it was
        # asked. The plan's counts lived here and made a RETRY under the same
        # idempotency key raise `OperationIdempotencyConflict`: the first run
        # converged six domains, the retry saw zero pending, hashed a different
        # request, and `execute_operation` refused to replay the answer it
        # already had. A key bound to a request that cannot be re-expressed is
        # not an idempotency key. The plan is recorded in the RESULT, where it
        # belongs -- it describes what happened, not what was requested.
        request_payload={"org_id": org_id, "reason": reason},
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=idempotency_key,
        trace_id=trace_id,
    )

    try:
        operation = execute_operation(conn, spec, mutation=mutation)
    except MasterDataConflict as exc:
        # The world moved between the plan and the write. A refusal, not a 500.
        raise ConvergenceRefused(str(exc)) from exc

    return {
        "operation_id": operation.operation_id,
        "outcome": operation.outcome,
        "result": operation.result,
        "idempotent_replay": operation.replayed,
    }


#: The registry label and the type declaration of each business identity kind,
#: read by the convergence AND by the direct creation command
#: (`core.master_data_commands`). ONE declaration, because a create that landed
#: in a registry of its own -- another label, another type version -- would give
#: the same business object two homes in the same organization, which is the
#: second authority this whole story exists to remove.
_BUSINESS_KINDS: dict[str, dict[str, Any]] = {
    DOMAIN_KIND: {
        "registry_label": DOMAIN_REGISTRY_LABEL,
        "type_label": "Business Domain",
        "type_description": (
            "An editable organization-owned root of the business taxonomy. The six "
            "supplied domains are seeds, never a closed list."
        ),
        "property_schema": DOMAIN_PROPERTY_SCHEMA,
        "relationships": DOMAIN_RELATIONSHIPS,
    },
    CLASSIFICATION_KIND: {
        "registry_label": CLASSIFICATION_REGISTRY_LABEL,
        "type_label": "Business Classification",
        "type_description": (
            "A named grouping inside a Business Domain. Its `classification_type` is "
            "the word the organization chose and is carried, never interpreted as a "
            "Product or an Activity."
        ),
        "property_schema": CLASSIFICATION_PROPERTY_SCHEMA,
        "relationships": CLASSIFICATION_RELATIONSHIPS,
    },
}


def ensure_business_registry(conn, *, org_id: str, object_kind: str, actor: str):
    """The organization's ONE registry for a business identity kind.

    Public because the convergence is no longer its only writer: since the
    creation gesture landed (`core.master_data_commands.create_business_identity`)
    a Business Domain can also be minted directly, and it has to land in the
    registry the convergence targets -- same object kind, same label, same
    version scope. Two entry points, one home.
    """

    if object_kind not in _BUSINESS_KINDS:
        raise MasterDataError(f"unknown business identity kind: {object_kind!r}")
    registry = fetch_org_registry(conn, org_id=org_id, object_kind=object_kind)
    if registry is not None:
        return registry
    return create_org_registry(
        conn,
        org_id=org_id,
        object_kind=object_kind,
        label=str(_BUSINESS_KINDS[object_kind]["registry_label"]),
        actor=actor,
        # Per identity. Under registry scope, publishing one Business Domain
        # would mint a version of every other one and require re-approving them
        # -- the exact shape migration 143 introduced `version_scope` to avoid.
        version_scope=VERSION_SCOPE_NODE,
    )


def ensure_business_type(conn, *, org_id: str, object_kind: str, actor: str):
    """The published type version a business identity's payload is checked against."""

    if object_kind not in _BUSINESS_KINDS:
        raise MasterDataError(f"unknown business identity kind: {object_kind!r}")
    declaration = _BUSINESS_KINDS[object_kind]
    return ensure_type_version(
        conn,
        org_id=org_id,
        object_kind=object_kind,
        label=str(declaration["type_label"]),
        description=str(declaration["type_description"]),
        property_schema=declaration["property_schema"],
        relationship_definitions=declaration["relationships"],
        origin="platform_template",
        actor=actor,
    )


def _apply(
    conn, *, org_id: str, plan: ConvergencePlan, actor: str, operation_id: str
) -> dict[str, Any]:
    domain_registry = ensure_business_registry(
        conn, org_id=org_id, object_kind=DOMAIN_KIND, actor=actor
    )
    classification_registry = ensure_business_registry(
        conn, org_id=org_id, object_kind=CLASSIFICATION_KIND, actor=actor
    )
    domain_type = ensure_business_type(
        conn, org_id=org_id, object_kind=DOMAIN_KIND, actor=actor
    )
    classification_type = ensure_business_type(
        conn, org_id=org_id, object_kind=CLASSIFICATION_KIND, actor=actor
    )

    converged_domains = 0
    for row in plan.domains:
        _converge_row(
            conn,
            org_id=org_id,
            registry_id=str(domain_registry["id"]),
            type_version_id=str(domain_type["id"]),
            node_kind=DOMAIN_KIND,
            source_table="mdm_business_domains",
            row=row,
            payload={
                "slug": row["slug"],
                "name": row["name"],
                "description": row["description"] or "",
                "owner": row["owner"],
                "lifecycle_state": row["status"],
            },
            parent_node_id=None,
            actor=actor,
            operation_id=operation_id,
        )
        converged_domains += 1

    converged_classifications = 0
    for row in plan.classifications:
        parent_node_id = str(row["parent_id"] or row["domain_id"])
        _converge_row(
            conn,
            org_id=org_id,
            registry_id=str(classification_registry["id"]),
            type_version_id=str(classification_type["id"]),
            node_kind=CLASSIFICATION_KIND,
            source_table="mdm_business_classifications",
            row=row,
            payload={
                "slug": row["slug"],
                "name": row["name"],
                "description": row["description"] or "",
                "owner": row["owner"],
                "lifecycle_state": row["status"],
                "classification_type": row["classification_type"],
                "domain_node_id": str(row["domain_id"]),
                "parent_node_id": str(row["parent_id"]) if row["parent_id"] else None,
            },
            parent_node_id=parent_node_id,
            actor=actor,
            operation_id=operation_id,
        )
        converged_classifications += 1

    # The moment the nodes exist is the moment the references ALREADY PUBLISHED
    # against them acquire an authority to be registered with. A Semantic Model
    # version that named a domain before this run is true and unrecorded, and
    # `master_data_used_by` is what the archive guard reads -- so a convergence
    # that minted the identities and left their dependents unwritten would hand
    # the authority a store that is empty for the wrong reason. Same transaction,
    # same actor, same audit row; a replay upserts the same rows onto themselves.
    from core.semantic_model_used_by import backfill_org_references  # noqa: PLC0415

    backfilled = backfill_org_references(conn, org_id=org_id, actor=actor)

    return {
        "converged_domains": converged_domains,
        "converged_classifications": converged_classifications,
        "domain_registry_id": str(domain_registry["id"]),
        "classification_registry_id": str(classification_registry["id"]),
        "domain_type_version_id": str(domain_type["id"]),
        "classification_type_version_id": str(classification_type["id"]),
        # Measured, not asserted: how many already-published Semantic Model
        # references entered the used-by store, and how many named a domain this
        # organization still does not govern.
        "registered_semantic_references": backfilled["registered"],
        "unregistered_semantic_references": backfilled["unregistered"],
    }


def _converge_row(
    conn,
    *,
    org_id: str,
    registry_id: str,
    type_version_id: str,
    node_kind: str,
    source_table: str,
    row: dict[str, Any],
    payload: dict[str, Any],
    parent_node_id: str | None,
    actor: str,
    operation_id: str,
) -> None:
    """Move ONE taxonomy row, keeping its identity and leaving it readable."""

    node_id = str(row["id"])
    # The NODE's existence is what is checked, never the stamp alone: an
    # interrupted run leaves a node whose source row is unstamped, and a check
    # on the stamp would mint the identity a second time -- which the primary
    # key would refuse, turning a resumable state into a permanent failure.
    node = fetch_org_node(conn, org_id=org_id, node_id=node_id)
    if node is None:
        node = create_org_node(
            conn,
            org_id=org_id,
            registry_id=registry_id,
            node_kind=node_kind,
            label=str(row["name"]),
            actor=actor,
            node_id=node_id,
        )
        version = create_node_version(
            conn,
            org_id=org_id,
            registry_id=registry_id,
            node_id=node_id,
            payload=payload,
            type_version_id=type_version_id,
            actor=actor,
        )
        if parent_node_id is not None:
            # The edge belongs to the CHILD's version: moving an object is a
            # change to the object that moved, so one regroup mints one version.
            version = replace_org_draft_memberships(
                conn,
                org_id=org_id,
                version_id=str(version["id"]),
                memberships=[
                    Membership(parent_node_id=parent_node_id, child_node_id=node_id)
                ],
            )
        publish_node_version(
            conn, org_id=org_id, version_id=str(version["id"]), actor=actor
        )
        if row["status"] == "archived":
            # Archived in the source is archived in the authority. The version
            # is published FIRST so the history exists before it is retired --
            # an identity archived with no published version is unreadable, and
            # AC3 forbids a state that makes an object unreadable.
            archive_org_node(conn, org_id=org_id, node_id=node_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.{source_table}
               SET superseded_at = NOW(),
                   superseded_by_node_id = %s,
                   superseded_by_operation_id = %s
             WHERE id = %s AND org_id = %s AND superseded_at IS NULL
            """,  # noqa: S608 -- source_table is a module-owned literal
            (node_id, operation_id, node_id, org_id),
        )


# ---------------------------------------------------------------------------
# The superseded store, written by the AUTHORITY and by nobody else.
#
# WHY AN INSERT EXISTS HERE AT ALL. Measured 2026-08-25: fourteen non-test
# readers resolve a Business Domain through `app.mdm_business_domains` -- the
# two Governance lenses, the Context Hub, `golden_questions`, `project_settings`
# applicability, `datastream_workbench`, `semantic_model`'s reference guard,
# `analyze_render_mcp`, `feedback_review`, `context_seed` -- and
# `business_taxonomy.create_link` refuses a link whose taxonomy row it cannot
# read. An identity minted only in `app.master_data_nodes` would therefore be
# invisible to every one of them and unlinkable: a creation gesture producing an
# object nobody can see or use.
#
# WHY IT IS NOT A SECOND AUTHORITY. The row is born SUPERSEDED -- it names the
# node that holds its authority and the operation that minted it, in the same
# transaction, from the same command -- which is character for character the
# state the convergence leaves behind. Nothing edits it except the command that
# edits the node beside it, so the two cannot drift; and when the lenses are
# re-pointed onto the authority these rows stop being read without changing
# anything about what they mean. The alternative -- re-point fourteen readers
# first -- is the named remainder of this wave, and until it lands it would have
# meant shipping a create gesture whose result no screen can show.
# ---------------------------------------------------------------------------

_PROJECTION_TABLE = {
    DOMAIN_KIND: "mdm_business_domains",
    CLASSIFICATION_KIND: "mdm_business_classifications",
}


def superseded_projection_table(object_kind: str) -> str:
    if object_kind not in _PROJECTION_TABLE:
        raise MasterDataError(f"unknown business identity kind: {object_kind!r}")
    return _PROJECTION_TABLE[object_kind]


def write_superseded_projection(
    conn,
    *,
    org_id: str,
    object_kind: str,
    node_id: str,
    operation_id: str,
    actor: str,
    payload: dict[str, Any],
) -> None:
    """Insert the readable projection of an identity the authority just minted."""

    table = superseded_projection_table(object_kind)
    if object_kind == DOMAIN_KIND:
        columns = "(id, org_id, slug, name, description, owner, status, created_by"
        values = (
            "(%(id)s, %(org)s, %(slug)s, %(name)s, %(description)s, %(owner)s, "
            "'active', %(actor)s"
        )
    else:
        columns = (
            "(id, org_id, domain_id, parent_id, classification_type, slug, name, "
            "description, owner, status, created_by"
        )
        values = (
            "(%(id)s, %(org)s, %(domain_id)s, %(parent_id)s, %(classification_type)s, "
            "%(slug)s, %(name)s, %(description)s, %(owner)s, 'active', %(actor)s"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.{table}
                {columns}, superseded_at, superseded_by_node_id, superseded_by_operation_id)
            VALUES {values}, NOW(), %(id)s, %(operation)s)
            """,  # noqa: S608 -- table and column lists are module-owned literals
            {
                "id": node_id,
                "org": org_id,
                "slug": payload["slug"],
                "name": payload["name"],
                "description": payload.get("description") or "",
                "owner": payload.get("owner"),
                "actor": actor,
                "domain_id": payload.get("domain_node_id"),
                "parent_id": payload.get("parent_node_id"),
                "classification_type": payload.get("classification_type"),
                "operation": operation_id,
            },
        )


def update_superseded_projection(
    conn,
    *,
    org_id: str,
    object_kind: str,
    node_id: str,
    name: str,
    description: str | None = None,
    classification_type: str | None = None,
) -> None:
    """Carry a rename into the projection, so no reader keeps the old name.

    `description=None` means the caller did not touch it, which is not the same
    as clearing it: a rename that blanked the description of every domain it
    renamed would be a silent erasure.
    """

    table = superseded_projection_table(object_kind)
    sets = ["name = %(name)s", "updated_at = NOW()"]
    if description is not None:
        sets.append("description = %(description)s")
    if object_kind == CLASSIFICATION_KIND and classification_type:
        sets.append("classification_type = %(classification_type)s")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.{table}
               SET {", ".join(sets)}
             WHERE id = %(id)s AND org_id = %(org)s
            """,  # noqa: S608 -- table and set list are module-owned literals
            {
                "id": node_id,
                "org": org_id,
                "name": name,
                "description": description,
                "classification_type": classification_type,
            },
        )


def set_superseded_projection_status(
    conn, *, org_id: str, object_kind: str, node_id: str, status: str
) -> None:
    """Archive or restore the projection with the identity it projects.

    Without it, the fourteen readers above keep serving an archived Business
    Domain as active -- and the Governance lens, which reads `status` from this
    row, would contradict the authority's own `archived_at` on the same object.
    """

    if status not in ("active", "archived"):
        raise MasterDataError(f"unknown lifecycle state: {status!r}")
    table = superseded_projection_table(object_kind)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.{table}
               SET status = %(status)s,
                   archived_at = CASE WHEN %(status)s = 'archived' THEN NOW() ELSE NULL END,
                   updated_at = NOW()
             WHERE id = %(id)s AND org_id = %(org)s
            """,  # noqa: S608 -- the table name is a module-owned literal
            {"id": node_id, "org": org_id, "status": status},
        )


__all__ = [
    "CLASSIFICATION_KIND",
    "CONVERGENCE_COMMAND",
    "DOMAIN_KIND",
    "ConvergencePlan",
    "ConvergenceRefused",
    "converge_org_taxonomy",
    "ensure_business_registry",
    "ensure_business_type",
    "plan_convergence",
    "set_superseded_projection_status",
    "superseded_projection_table",
    "update_superseded_projection",
    "write_superseded_projection",
]
