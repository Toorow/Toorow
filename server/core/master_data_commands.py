"""The auditable half of the Master Data lifecycle (Story 49.2).

`governance.md` `Master Data` requires that every object have "stable identity,
scope, owner, status and an **auditable lifecycle**", and that "deletion, merge
or regrouping is guarded when consumers would change meaning".

`core.master_data` owns the guard: :func:`~core.master_data.archive_node` refuses
while live consumers exist, and :func:`~core.master_data.assess_node_impact`
fails closed when the used-by store cannot be read. What was missing is the other
half of the sentence -- those commands wrote nothing to the audit trail and no
route could reach them.

WHAT THE CUTOVER OF 2026-08-25 ADDED HERE
-----------------------------------------
The four legacy identity writers of `core.business_taxonomy` refuse from now on,
and the sentence they refuse with names two gestures in order: *converge this
organization, then create, rename or archive it in Master Data*. The second half
of that sentence did not exist -- `SUPPORTED_ACTIONS` was `archive` and `restore`
-- so a converged organization could not create or rename a Business Domain at
all. It can now, through :func:`create_business_identity` and the `rename`
action, and both land in exactly the registry, type version and payload shape
`core.master_data_convergence` writes: two entry points, one home.

Why this module rather than a third change-set store
----------------------------------------------------
Two change-set lifecycles already exist: `app.project_change_sets` (Story 46.3,
Project capability intent) and `app.controls_change_sets` (Story 49.4, Controls &
Quality). Story 49.2 forbids adding a parallel authority, and `governance.md`
does not prescribe a change set for Master Data -- it prescribes an *auditable*
lifecycle and a guard.

So these commands route through `core.operations.execute_operation`, the generic
durable-operation wrapper (AD-27) that every governed act in this repository
already uses: mutation, audit row and outbox event commit in ONE transaction, and
a replayed idempotency key returns the first result instead of acting twice.

What the audit record has to contain
------------------------------------
The impact is written into the operation result on BOTH paths. A refusal that
leaves no trace is indistinguishable from a command nobody ran, and an
acknowledged archive that does not record what was acknowledged turns a human
decision into an unexplained state change. So:

* a refusal raises before any write, and the caller renders the named consumers;
* an acknowledged archive records the consumers it went ahead despite.

THE ARCHIVE GUARD OF A BUSINESS IDENTITY, AND WHERE ITS TWO HALVES LIVE
-----------------------------------------------------------------------
Until migration 311 (2026-08-25) the used-by store could not hold a single row
about a converged Business Domain: its composite foreign key pointed at
`master_data_nodes(project_id, id)` while an organization node carries
`project_id IS NULL`. The hole `governance.md` named that morning -- *"no writer
registers a Semantic Model version's Business Domain reference there yet"* -- was
therefore a SCHEMA hole under a missing writer. Both landed the same day:
`semantic_model_used_by.register_published_version` writes the references inside
`confirm_change_set`, and `master_data.assess_org_node_impact` reads them.

So this module's guard is two halves, and it owns only the second:

* `master_data.assess_org_node_impact` -- the used-by store's OWN organization
  reader. Calling it rather than re-writing its query is what keeps one answer to
  "who depends on this identity";
* `app.mdm_business_links` -- the live Context Hub links, which the convergence
  deliberately does not move and the used-by store therefore never holds. It is
  the same count the Governance lens prints in its `Used by` column, so the
  screen and the refusal cannot disagree.

`business_taxonomy.domain_used_by` is deliberately NOT read here. It answers the
same question as the used-by rows now -- the Semantic Model versions naming a
domain -- so reading both would count every consumer twice and refuse an archive
naming two of everything.
"""

from __future__ import annotations

import logging
from typing import Any

from core.master_data import (
    MasterDataConflict,
    MasterDataError,
    MasterDataNotFound,
    MasterDataUnavailable,
    Membership,
    NodeImpact,
    archive_node,
    archive_org_node_guarded,
    content_hash,
    create_node_version,
    create_org_node,
    current_node_version_id,
    current_node_versions,
    fetch_node,
    fetch_org_memberships,
    fetch_org_node,
    node_versions_per_identity,
    publish_node_version,
    rename_node,
    rename_org_node,
    replace_org_draft_memberships,
    restore_node,
    restore_org_node,
)
from core.master_data_convergence import (
    CLASSIFICATION_KIND,
    DOMAIN_KIND,
    ensure_business_registry,
    ensure_business_type,
    plan_convergence,
    set_superseded_projection_status,
    update_superseded_projection,
    write_superseded_projection,
)
from core.operations import (
    MutationResult,
    OperationSpec,
    execute_operation,
    operation_already_ran,
)

logger = logging.getLogger(__name__)

MASTER_DATA_NODE_COMMAND = "governance.master_data.node"

#: What a caller may ask of a node. `merge` and `regroup` are deliberately absent:
#: they change what existing rows MEAN, so they need a diff and a preview before
#: a confirmation, not a single call. Naming the gap here keeps it visible.
#:
#: `create` and `rename` arrived with the cutover of 2026-08-25: the legacy
#: writers refuse, and a refusal that names a gesture which does not exist is a
#: dead end rather than a repair.
SUPPORTED_ACTIONS = ("create", "rename", "archive", "restore")

#: The actions this module reaches through an EXISTING node id. `create` is not
#: one of them -- there is no id to address yet -- so it has its own function and
#: its own route.
NODE_ADDRESSED_ACTIONS = ("rename", "archive", "restore")

#: The two identity kinds the business taxonomy converges into, and the only two
#: this command mints. Anything else in `app.master_data_nodes` is a Project
#: registry's object (a market, a competitor) whose creation belongs to the
#: capability that owns it.
BUSINESS_IDENTITY_KINDS = (DOMAIN_KIND, CLASSIFICATION_KIND)

_KIND_LABEL = {DOMAIN_KIND: "Business Domain", CLASSIFICATION_KIND: "Classification"}

#: A refusal a caller must be able to tell apart from an impact refusal: the
#: repair is a different gesture, run by a different person, on another screen.
CONVERGE_FIRST_CODE = "master_data_organization_not_converged"

#: The same two gestures, in the same order, as
#: `business_taxonomy.LEGACY_TAXONOMY_WRITE_REFUSED_MESSAGE`. It names no store,
#: no column and no deployment state.
CONVERGE_FIRST_MESSAGE = (
    "This organization has not converged into Master Data yet, so nothing can be "
    "declared here. Run the convergence on the Governance Master Data screen first "
    "-- it keeps every Business Domain and classification at the identity it "
    "already has -- then create, rename or archive it here."
)

DUPLICATE_SLUG_CODE = "master_data_short_code_taken"

#: The refusal a caller reads when the object moved under it. The name is the one
#: this repository already uses for that answer -- `context_api` refuses a stale
#: topic or procedure edit under `version_conflict` -- because a second word for
#: one meaning makes every screen choose which of the two to render.
STALE_VERSION_CODE = "version_conflict"

#: It names the state and the gesture, and it never says "could not be saved":
#: a refusal to OVERWRITE and a failure to save call for opposite moves.
STALE_VERSION_MESSAGE = (
    "This identity has been revised since you read it, so nothing was changed. "
    "Reload it, read the current version, and apply your change on top of that one."
)


class _Unstated:
    """No precondition was stated AT ALL -- which is not "I read no revision".

    A rename must state the base it is renaming from, and `None` is a legitimate
    base to have read: a Project-scoped identity versions by registry, so its
    node holds no published revision. Defaulting the parameter to `None` would
    make those two cases one, and a caller that simply forgot would be read as a
    caller that looked and found nothing -- a guard that cannot fire.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unstated>"


UNSTATED = _Unstated()


class MasterDataCommandRefused(Exception):
    """The command was refused before any write. The caller sees 409, not 500."""

    def __init__(
        self,
        message: str,
        *,
        impact: dict[str, Any] | None = None,
        code: str = "master_data_command_refused",
    ):
        super().__init__(message)
        self.impact = impact or {}
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "impact": self.impact,
        }


# ---------------------------------------------------------------------------
# Reading who still depends on an identity.
# ---------------------------------------------------------------------------


def _read(conn, sql: str, params: Any, what: str) -> list[tuple]:
    """One read whose failure is an OUTAGE, never an encouraging empty list."""

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 -- any read failure fails closed here
        raise MasterDataUnavailable(
            f"{what} could not be read: {type(exc).__name__}"
        ) from exc


def assess_org_identity_impact(conn, *, org_id: str, node_id: str) -> NodeImpact:
    """Live consumers of ONE organization identity, or an outage.

    TWO STORES, AND ONLY THE SECOND ONE BELONGS HERE. The used-by half is read by
    `master_data.assess_org_node_impact` -- the store's own organization reader --
    because two readers of one store drift into two answers to one question. What
    this function adds is the store the convergence deliberately does NOT move:
    the governed Context Hub links.
    """

    from core.master_data import assess_org_node_impact  # noqa: PLC0415

    registered = assess_org_node_impact(conn, org_id=org_id, node_id=node_id)
    consumers: list[dict[str, Any]] = list(registered.consumers)

    # The governed Context Hub links. A withdrawn link is not a consumer
    # (migration 306), which is the whole point of retiring rather than deleting.
    for row in _read(
        conn,
        """
        SELECT target_type, target_id
          FROM app.mdm_business_links
         WHERE org_id = %s AND taxonomy_id = %s AND retired_at IS NULL
         ORDER BY target_type, target_id
        """,
        (org_id, node_id),
        "the business links",
    ):
        consumers.append(
            {
                "consumer_kind": str(row[0]),
                "consumer_id": str(row[1]),
                "consumer_label": None,
                "consumer_version_id": None,
            }
        )

    return NodeImpact(node_id=node_id, consumers=tuple(consumers))


# ---------------------------------------------------------------------------
# Creating a business identity.
# ---------------------------------------------------------------------------


def _mint_identity_id(kind: str) -> str:
    """A carried-shape id, because the projection row shares it.

    Migration 307 constrains a node id to `(mdnode|bdm|bd|bcl)_(ULID|md5 slice)`.
    A created Business Domain takes `bd_`, a classification `bcl_` -- the same
    shapes the convergence carries -- so a row of the superseded store and the
    node that owns it are one id, exactly as they are for a converged identity.
    """

    from ulid import ULID  # noqa: PLC0415 -- the repository's minting library

    # Two literal prefixes, not one conditional one: the mint scanner
    # (test_visualization_options_labels) follows a prefix to its literal.
    if kind == DOMAIN_KIND:
        return f"bd_{ULID()}"
    return f"bcl_{ULID()}"


def _converge_first_refusal(conn, *, org_id: str) -> MasterDataCommandRefused | None:
    """The convergence refusal this organization is owed, or None.

    ONE GATE FOR EVERY COMMAND THAT NEEDS IT. Ratified 2026-08-25: *"an
    unconverged organization converges first, then writes through the Master Data
    authority"*, and the *Incomplete if* of that amendment names the whole class
    -- *"a create, rename, archive or restore runs on an organization that has
    not converged, or its refusal names anything other than the convergence"*.
    Until 2026-08-31 only `create` passed through here. `rename`, `archive` and
    `restore` reached an organization identity that the authority does not hold
    yet, found no node, and answered `MasterDataNotFound` -> **404 "Governance
    object not found"** about each of the six seeded Business Domains the lens
    itself lists. A screen that lists an object and then denies it exists names
    no gesture at all; the sentence below names two, in order.

    It RETURNS the refusal rather than raising it so both callers can place it
    where their own replay question is answered -- a refusal must not mint an
    operation row, and a retry of a command that already ran must be answered by
    the operation it repeats rather than refused a second time.
    """

    plan = plan_convergence(conn, org_id=org_id)
    if plan.is_empty:
        return None
    return MasterDataCommandRefused(
        CONVERGE_FIRST_MESSAGE,
        code=CONVERGE_FIRST_CODE,
        impact={"pending": plan.as_dict()},
    )


def _require_converged(conn, *, org_id: str) -> None:
    """The same gate, raised. The console does not draw the create control on an
    unconverged organization; this is that rule at the threshold, so a script or
    a future MCP tool meets it too."""

    refusal = _converge_first_refusal(conn, org_id=org_id)
    if refusal is not None:
        raise refusal


def _assert_short_code_is_free(conn, *, org_id: str, kind: str, slug: str) -> None:
    from core.master_data_convergence import superseded_projection_table  # noqa: PLC0415

    table = superseded_projection_table(kind)
    rows = _read(
        conn,
        f"SELECT id FROM app.{table} WHERE org_id = %s AND slug = %s",  # noqa: S608
        (org_id, slug),
        "the business taxonomy",
    )
    if rows:
        raise MasterDataCommandRefused(
            f"A {_KIND_LABEL[kind]} already uses the short code “{slug}”. "
            "Choose another short code and submit again.",
            code=DUPLICATE_SLUG_CODE,
        )


def _require_live_identity(conn, *, org_id: str, node_id: str, kind: str) -> dict[str, Any]:
    node = fetch_org_node(conn, org_id=org_id, node_id=node_id)
    if node is None or node.get("node_kind") != kind:
        raise MasterDataNotFound(f"{_KIND_LABEL[kind]} not found in this organization")
    if node.get("archived_at") is not None:
        raise MasterDataCommandRefused(
            f"That {_KIND_LABEL[kind]} is archived, so nothing can be filed under it. "
            "Restore it first, or choose another one.",
        )
    return node


def create_business_identity(
    conn,
    *,
    project_id: str,
    org_id: str,
    kind: str,
    name: str,
    actor: str,
    idempotency_key: str,
    reason: str = "",
    slug: str | None = None,
    description: str = "",
    owner: str | None = None,
    classification_type: str | None = None,
    domain_node_id: str | None = None,
    parent_node_id: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Mint ONE business identity in the authority, as a durable operation.

    It lands where the convergence lands: the organization registry for its kind
    (`ensure_business_registry`), the type version that declares its payload
    (`ensure_business_type`), a published node version carrying the same fields
    the convergence carries, and -- for a classification -- the membership edge on
    its OWN version, because moving an object is a change to the object that
    moved.

    Raises:
        ValueError: an unknown kind, or a name that is not one.
        MasterDataCommandRefused: the organization has not converged, or the
            short code is taken. Raised BEFORE `execute_operation`, so a refusal
            never mints an operation row.
    """

    from core.business_taxonomy import normalize_slug  # noqa: PLC0415 -- a validator

    kind = (kind or "").strip()
    if kind not in BUSINESS_IDENTITY_KINDS:
        raise ValueError(f"kind must be one of {list(BUSINESS_IDENTITY_KINDS)}")
    label = (name or "").strip()
    if not label:
        raise ValueError("name is required")
    short_code = normalize_slug(slug or label, "slug")

    payload: dict[str, Any] = {
        "slug": short_code,
        "name": label,
        "description": (description or "").strip(),
        "owner": (owner or None),
        "lifecycle_state": "active",
    }
    parent_of_edge: str | None = None
    if kind == CLASSIFICATION_KIND:
        type_word = (classification_type or "").strip()
        if not type_word:
            raise ValueError("classification_type is required")
        if not (domain_node_id or "").strip():
            raise ValueError("domain_node_id is required")
        payload.update(
            {
                # Carried, never interpreted -- the Implementation Gate's item 4.
                "classification_type": type_word,
                "domain_node_id": domain_node_id.strip(),
                "parent_node_id": parent_node_id.strip() if parent_node_id else None,
            }
        )
        parent_of_edge = (parent_node_id or domain_node_id).strip()

    def mutation(mutation_conn, operation_id: str) -> MutationResult:
        registry = ensure_business_registry(
            mutation_conn, org_id=org_id, object_kind=kind, actor=actor
        )
        type_version = ensure_business_type(
            mutation_conn, org_id=org_id, object_kind=kind, actor=actor
        )
        node_id = _mint_identity_id(kind)
        create_org_node(
            mutation_conn,
            org_id=org_id,
            registry_id=str(registry["id"]),
            node_kind=kind,
            label=label,
            actor=actor,
            node_id=node_id,
        )
        version = create_node_version(
            mutation_conn,
            org_id=org_id,
            registry_id=str(registry["id"]),
            node_id=node_id,
            payload=payload,
            type_version_id=str(type_version["id"]),
            actor=actor,
        )
        if parent_of_edge is not None:
            version = replace_org_draft_memberships(
                mutation_conn,
                org_id=org_id,
                version_id=str(version["id"]),
                memberships=[
                    Membership(parent_node_id=parent_of_edge, child_node_id=node_id)
                ],
            )
        publish_node_version(
            mutation_conn, org_id=org_id, version_id=str(version["id"]), actor=actor
        )
        write_superseded_projection(
            mutation_conn,
            org_id=org_id,
            object_kind=kind,
            node_id=node_id,
            operation_id=operation_id,
            actor=actor,
            payload=payload,
        )
        return MutationResult(
            outcome="succeeded",
            # Nothing existed before, and that is the honest before-state: the
            # identity is the after.
            before_hash=content_hash({"org_id": org_id, "kind": kind, "slug": short_code}),
            after_hash=content_hash({"node_id": node_id, **payload}),
            result={
                "node_id": node_id,
                "action": "create",
                "kind": kind,
                "label": label,
                "slug": short_code,
                "registry_id": str(registry["id"]),
                "version_id": str(version["id"]),
            },
            outbox_payload={
                "org_id": org_id,
                "project_id": project_id,
                "node_id": node_id,
                "kind": kind,
                "action": "create",
            },
        )

    spec = OperationSpec(
        command_type=MASTER_DATA_NODE_COMMAND,
        actor=actor,
        effective_org_id=org_id,
        # The identity does not exist yet, so the path names what is being
        # claimed -- a kind and a short code -- rather than an id minted inside
        # the transaction. A retry addresses the same claim.
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            "governance:master-data",
            f"{kind}:{short_code}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "master-data-v1",
            "catalog": "master-data-v1",
            "tool": "master-data-v1",
        },
        request_payload={
            "action": "create",
            "kind": kind,
            "slug": short_code,
            "name": label,
            "reason": reason,
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=idempotency_key,
        trace_id=trace_id,
    )

    # THE PRE-CHECKS READ THE WORLD, SO A RETRY MUST NOT MEET THEM. The first
    # attempt took the short code; a client that timed out and retried under the
    # SAME key would then be told its own creation is a duplicate -- a refusal
    # naming a repair (choose another short code) that would mint the second
    # identity the key exists to prevent. When the key has already produced an
    # operation, the replay path answers instead.
    if not operation_already_ran(conn, spec):
        _require_converged(conn, org_id=org_id)
        _assert_short_code_is_free(conn, org_id=org_id, kind=kind, slug=short_code)
        if kind == CLASSIFICATION_KIND:
            _require_live_identity(
                conn,
                org_id=org_id,
                node_id=str(payload["domain_node_id"]),
                kind=DOMAIN_KIND,
            )
            if payload.get("parent_node_id"):
                _require_live_identity(
                    conn,
                    org_id=org_id,
                    node_id=str(payload["parent_node_id"]),
                    kind=CLASSIFICATION_KIND,
                )

    try:
        operation = execute_operation(conn, spec, mutation=mutation)
    except MasterDataConflict as exc:
        raise MasterDataCommandRefused(str(exc)) from exc

    return {
        "operation_id": operation.operation_id,
        "outcome": operation.outcome,
        "result": operation.result,
        "idempotent_replay": operation.replayed,
    }


# ---------------------------------------------------------------------------
# Commanding an identity that already exists.
# ---------------------------------------------------------------------------


def _republish(
    conn,
    *,
    org_id: str,
    node: dict[str, Any],
    actor: str,
    changes: dict[str, Any],
) -> str:
    """Mint and publish the next version of ONE identity, carrying its edges.

    The edges are re-attached deliberately: a membership belongs to a VERSION
    (`replace_org_draft_memberships`), so a new version published without them
    would detach a classification from its Business Domain -- a rename that
    silently regroups. Returns the new version id.

    IT MINTS EVEN WHEN THERE IS NOTHING TO CARRY FORWARD (AI-328). This used to
    return None for an identity with no current revision, and that absence was
    permanent: a Product minted by `observed_entities.attach_observed_entity`
    carries no version at all, so its base was None, every rename stated None,
    every rename matched, and the optimistic lock of 2026-08-30 could not fire
    ONCE in that object's life -- two renames from one base both landed. A guard
    that cannot fire reads as protection and is worse than none, in this
    document's own words. The first revision is therefore minted here, and from
    it onwards the lock has a base that MOVES.

    `changes` IS ONLY WHAT THE IDENTITY'S TYPE DECLARES. A Business Domain and a
    classification carry `name` and `lifecycle_state` in their payload because
    their type version requires them (`master_data_convergence`'s property
    schemas), so a rename writes the name in and the two cannot disagree. A
    client object's payload is the attributes bag its feed landed
    (`entity_reference_import.build_node_payload`), and writing a name beside it
    would be a key the very next import drops -- a history that records a rename
    and then silently un-records it. So for those the payload is carried forward
    unchanged: the new revision has the same content digest, which is the honest
    statement that the governed CONTENT did not change. What changed is the
    label, which lives on the node, travels in the operation's before/after
    hashes, and is what the reader is shown.
    """

    current = current_node_versions(
        conn,
        org_id=org_id,
        registry_id=str(node["registry_id"]),
        node_ids=[str(node["id"])],
    ).get(str(node["id"]))

    payload = dict((current or {}).get("payload") or {})
    payload.update(changes)
    edges = (
        fetch_org_memberships(conn, version_id=str(current["id"]))
        if current is not None
        else ()
    )
    version = create_node_version(
        conn,
        org_id=org_id,
        registry_id=str(node["registry_id"]),
        node_id=str(node["id"]),
        payload=payload,
        type_version_id=(current or {}).get("type_version_id"),
        actor=actor,
    )
    if edges:
        version = replace_org_draft_memberships(
            conn,
            org_id=org_id,
            version_id=str(version["id"]),
            memberships=list(edges),
        )
    publish_node_version(conn, org_id=org_id, version_id=str(version["id"]), actor=actor)
    return str(version["id"])


def run_node_command(
    conn,
    *,
    project_id: str,
    org_id: str,
    node_id: str,
    action: str,
    actor: str,
    idempotency_key: str,
    acknowledge_impact: bool = False,
    label: str | None = None,
    description: str | None = None,
    classification_type: str | None = None,
    expected_version: str | None | _Unstated = UNSTATED,
    reason: str = "",
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Run one guarded Master Data node command as a durable operation.

    TWO SCOPES, ONE COMMAND. A Project registry's identity (a market, a
    competitor) lives at `project_id`; a converged Business Domain is an
    ORGANIZATION identity with `project_id IS NULL`. Every writer in
    `core.master_data` comes in two shapes for that reason, and this function
    used to call only the Project half -- so archiving a converged Business
    Domain updated no row and answered "node not found" for an object the screen
    had just listed.

    THE RENAME STATES THE BASE IT RENAMES FROM. `expected_version` is the id of
    the revision the caller READ (`master_data.current_node_version_id`), and it
    is REQUIRED for `rename`: two people editing one identity from two screens
    were arbitrated by nothing but who pressed Save last, and the loser's change
    disappeared without either of them being told. It is honoured wherever a
    caller states it and required only here, because `archive` and `restore`
    carry a guard of their own -- an archive is refused while a consumer names
    the identity, whichever revision it is at.

    The check runs TWICE, and both are needed. Once before `execute_operation`,
    so an ordinary stale save is refused without minting an operation row -- the
    same shape as the impact refusal. Once INSIDE the mutation, under
    `FOR UPDATE` on the revision itself, because that is the only reading that
    can decide a RACE: without the lock two renames read the same base, both
    pass, and both publish.

    AND THE LOCK NEEDS A BASE THAT MOVES, WHICH IS WHY A RENAME MINTS A VERSION
    FOR ALL THREE KINDS (AI-328). The precondition was written for the two
    business identities and reached the Products and Activities of the same
    Overview inert: their rename republished nothing, their base stayed at
    whatever it was -- None, for an identity minted by an observation -- and two
    renames from one base both landed while the screen promised a version. The
    republish is now decided by the registry's `version_scope`, so every
    identity that HAS a history of its own gets a revision when it is renamed,
    and the second rename from a spent base is refused. Country is untouched: its
    registry versions as one grouping, and it says so in the same column.

    Raises:
        ValueError: unknown action, or a rename that stated no base.
        MasterDataCommandRefused: the organization has not converged
            (`CONVERGE_FIRST_CODE`, the same gate the create door holds), live
            consumers block an unacknowledged archive, or the identity has been
            revised since the caller read it (`STALE_VERSION_CODE`). Raised
            BEFORE `execute_operation` wherever it can be, so a refusal never
            mints an operation row -- a refused command is not an act that
            happened.
        MasterDataNotFound: no such identity in this Project or organization.
        MasterDataUnavailable: a store the guard reads could not be read.
    """
    action = (action or "").strip()
    if action not in NODE_ADDRESSED_ACTIONS:
        raise ValueError(f"action must be one of {list(NODE_ADDRESSED_ACTIONS)}")

    org_node = fetch_org_node(conn, org_id=org_id, node_id=node_id)
    node_before = org_node or fetch_node(conn, project_id=project_id, node_id=node_id)
    if node_before is None:
        # THE GATE BEFORE THE 404, and the order is the whole repair. On an
        # organization that has not converged, the six seeded Business Domains
        # the Governance lens LISTS are legacy rows and no node holds them -- so
        # "not found" was the answer to every rename, archive and restore on an
        # object the screen had just shown. The convergence refusal names the two
        # gestures that reach the identity; a 404 names none.
        _require_converged(conn, org_id=org_id)
        # Said once, here, rather than by whichever writer happened to update no
        # row: a command addressed at an identity that does not exist in this
        # scope is not a failed write, it is a caller looking at the wrong thing.
        raise MasterDataNotFound("node not found in this Project or organization")
    node_kind = str(node_before["node_kind"])
    is_business_identity = org_node is not None and node_kind in BUSINESS_IDENTITY_KINDS
    new_label = (label or "").strip()
    if action == "rename" and not new_label:
        raise ValueError("label is required to rename an identity")
    if action == "rename" and isinstance(expected_version, _Unstated):
        raise ValueError(
            "expected_version is required to rename an identity: send back the "
            "version identity you read"
        )
    # A RENAME MINTS A VERSION WHEREVER THE IDENTITY HAS A HISTORY OF ITS OWN
    # (AI-328), and the registry's `version_scope` is what says so -- not the
    # node's kind. Migration 143 introduced that column for these three exactly:
    # "node: each identity has its own lifecycle (Business Domains, Products,
    # Activities)". Read only for a rename, because it is the only action whose
    # route it decides: an archive and a restore are read from the node's
    # `archived_at`, which is the one lifecycle word all three kinds report from.
    # And read only once the rename's own two preconditions have passed, so a
    # malformed command still touches no store.
    versions_per_identity = action == "rename" and node_versions_per_identity(
        conn, node_id=node_id
    )
    checks_version = not isinstance(expected_version, _Unstated)
    #: `""` is not an identity and not an honest absence, so it is read as the
    #: absence -- a caller that sends it is stating "the object I read had no
    #: published revision", and is held to exactly that.
    stated_version: str | None = None
    if checks_version and expected_version is not None:
        stated_version = str(expected_version).strip() or None

    # The impact is read BEFORE the operation, for two reasons: a refusal must
    # not leave an operation row behind, and the acknowledged path has to record
    # exactly what was acknowledged rather than re-reading it after the write,
    # when the answer would already have changed.
    if org_node is not None:
        impact = assess_org_identity_impact(conn, org_id=org_id, node_id=node_id)
    else:
        from core.master_data import assess_node_impact  # noqa: PLC0415

        impact = assess_node_impact(conn, project_id=project_id, node_id=node_id)

    # THE CONVERGENCE COMES FIRST, and it is the same gate the create door holds
    # (`_converge_first_refusal`). An organization whose taxonomy is half in the
    # legacy store is written by ONE authority or by none, so a rename that would
    # publish a node version while unconverged rows still exist beside it is
    # refused with the convergence rather than performed. It is asked only of a
    # business identity: a Project registry's object (a market, a competitor) is
    # not part of the taxonomy this convergence moves and never waits on it.
    refusal: MasterDataCommandRefused | None = (
        _converge_first_refusal(conn, org_id=org_id) if is_business_identity else None
    )
    if refusal is None and action == "archive" and not impact.is_clear and not acknowledge_impact:
        refusal = MasterDataCommandRefused(
            f"node {node_id} is still used by {impact.describe()}. "
            "Archiving it would break those consumers. Release them, or confirm "
            "the archive with the impact acknowledged.",
            impact={"consumers": list(impact.consumers), "summary": impact.describe()},
        )

    # THE STALE BASE IS READ HERE, WITH THE IMPACT, AND REFUSED IN THE SAME PLACE
    # BELOW -- past the replay question. A retry of a rename that already
    # succeeded reads a base the first attempt itself moved, so refusing it eagerly
    # would tell the person to reload after their own change landed: the exact
    # shape `test_a_retried_archive_replays_instead_of_being_refused_by_a_new_consumer`
    # holds for the archive guard.
    #
    # This reading cannot decide a RACE; the locked one inside the mutation does.
    # It is here so an ordinary stale save is refused without minting an operation
    # row at all.
    if (
        refusal is None
        and checks_version
        and current_node_version_id(conn, org_id=org_id, node_id=node_id) != stated_version
    ):
        refusal = MasterDataCommandRefused(STALE_VERSION_MESSAGE, code=STALE_VERSION_CODE)

    def mutation(mutation_conn, _operation_id: str) -> MutationResult:
        version_id: str | None = None
        if checks_version:
            # THE ONE THAT DECIDES A RACE. `FOR UPDATE` holds the revision row,
            # so a second command on the same identity waits here and reads what
            # the first left behind rather than the base they both started from.
            # Raised inside the mutation deliberately: `execute_operation` has
            # already inserted the operation row, and the caller's transaction
            # rolls back with it, so the refusal still writes nothing.
            if (
                current_node_version_id(
                    mutation_conn, org_id=org_id, node_id=node_id, for_update=True
                )
                != stated_version
            ):
                raise MasterDataCommandRefused(
                    STALE_VERSION_MESSAGE, code=STALE_VERSION_CODE
                )
        if action == "rename":
            changes: dict[str, Any] = {}
            if org_node is not None:
                node = rename_org_node(
                    mutation_conn, org_id=org_id, node_id=node_id, label=new_label
                )
            else:
                node = rename_node(
                    mutation_conn, project_id=project_id, node_id=node_id, label=new_label
                )
            if is_business_identity:
                # The two fields a business identity's TYPE declares beside its
                # name. They are written into the payload so the projection, the
                # node and the revision cannot disagree about one identity.
                changes = {"name": new_label}
                if description is not None:
                    changes["description"] = description.strip()
                if classification_type and node_kind == CLASSIFICATION_KIND:
                    changes["classification_type"] = classification_type.strip()
            if versions_per_identity:
                version_id = _republish(
                    mutation_conn,
                    org_id=org_id,
                    node=node,
                    actor=actor,
                    changes=changes,
                )
            if is_business_identity:
                update_superseded_projection(
                    mutation_conn,
                    org_id=org_id,
                    object_kind=node_kind,
                    node_id=node_id,
                    name=new_label,
                    description=(
                        description.strip() if description is not None else None
                    ),
                    classification_type=classification_type,
                )
            # The label as it was BEFORE the update, read before the mutation:
            # taking it from the returned row would hash the new name twice and
            # record a change that looks like none.
            before = {"label": str(node_before["label"])}
            after = {"label": new_label}
        elif action == "archive":
            if org_node is not None:
                # The GUARDED writer, with the reading taken above rather than a
                # second one: the guard and the decision shown to the human must
                # be the same evidence. It is the same shape the Project-scoped
                # branch below uses, and passing `impact` is what makes the two
                # agree -- this command's impact widens the store's own reading
                # with the governed links.
                node = archive_org_node_guarded(
                    mutation_conn,
                    org_id=org_id,
                    node_id=node_id,
                    acknowledge_impact=acknowledge_impact,
                    impact=impact,
                )
                if is_business_identity:
                    version_id = _republish(
                        mutation_conn,
                        org_id=org_id,
                        node=node,
                        actor=actor,
                        changes={"lifecycle_state": "archived"},
                    )
                    set_superseded_projection_status(
                        mutation_conn,
                        org_id=org_id,
                        object_kind=node_kind,
                        node_id=node_id,
                        status="archived",
                    )
            else:
                node = archive_node(
                    mutation_conn,
                    project_id=project_id,
                    node_id=node_id,
                    acknowledge_impact=acknowledge_impact,
                    # The reading taken above, not a second one: the guard and the
                    # decision shown to the human must be the same evidence.
                    impact=impact,
                )
            before, after = {"archived": False}, {"archived": True}
        else:
            if org_node is not None:
                node = restore_org_node(mutation_conn, org_id=org_id, node_id=node_id)
                if is_business_identity:
                    version_id = _republish(
                        mutation_conn,
                        org_id=org_id,
                        node=node,
                        actor=actor,
                        changes={"lifecycle_state": "active"},
                    )
                    set_superseded_projection_status(
                        mutation_conn,
                        org_id=org_id,
                        object_kind=node_kind,
                        node_id=node_id,
                        status="active",
                    )
            else:
                node = restore_node(mutation_conn, project_id=project_id, node_id=node_id)
            before, after = {"archived": True}, {"archived": False}

        return MutationResult(
            outcome="succeeded",
            before_hash=content_hash({"node_id": node_id, **before}),
            after_hash=content_hash({"node_id": node_id, **after}),
            result={
                "node_id": node_id,
                "action": action,
                "label": node.get("label"),
                "registry_id": node.get("registry_id"),
                "version_id": version_id,
                # Recorded on BOTH paths, and `[]` here is a read that returned
                # none -- the impact readers raise rather than returning an
                # empty tuple when a store is unreadable.
                "consumers_at_command_time": list(impact.consumers),
                "impact_acknowledged": bool(acknowledge_impact and not impact.is_clear),
            },
            outbox_payload={
                "project_id": project_id,
                "node_id": node_id,
                "action": action,
            },
        )

    spec = OperationSpec(
        command_type=MASTER_DATA_NODE_COMMAND,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            "governance:master-data",
            f"node:{node_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context or {},
        versions={
            "policy": "master-data-v1",
            "catalog": "master-data-v1",
            "tool": "master-data-v1",
        },
        request_payload={
            "node_id": node_id,
            "action": action,
            "acknowledge_impact": bool(acknowledge_impact),
            **({"label": new_label} if action == "rename" else {}),
            **({"reason": reason} if reason else {}),
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=idempotency_key,
        trace_id=trace_id,
    )

    # THE REFUSAL IS RAISED HERE, BEFORE THE OPERATION AND AFTER THE REPLAY
    # QUESTION. A refusal must not mint an operation row -- it is not an act that
    # happened -- and a RETRY of an archive that already succeeded must not be
    # refused because a consumer appeared in the meantime: the command it repeats
    # ran when the guard was clear, and the key exists so it answers with what
    # happened rather than acting, or refusing, a second time.
    if refusal is not None and not operation_already_ran(conn, spec):
        raise refusal

    try:
        operation = execute_operation(conn, spec, mutation=mutation)
    except MasterDataConflict as exc:
        # The guard inside archive_node fired despite the pre-check: the world
        # moved between the two reads. Surfaced as a refusal, not a 500.
        raise MasterDataCommandRefused(str(exc)) from exc

    return {
        "operation_id": operation.operation_id,
        "outcome": operation.outcome,
        "result": operation.result,
        "idempotent_replay": operation.replayed,
    }


__all__ = [
    "BUSINESS_IDENTITY_KINDS",
    "CONVERGE_FIRST_CODE",
    "CONVERGE_FIRST_MESSAGE",
    "DUPLICATE_SLUG_CODE",
    "MASTER_DATA_NODE_COMMAND",
    "NODE_ADDRESSED_ACTIONS",
    "SUPPORTED_ACTIONS",
    "MasterDataCommandRefused",
    "MasterDataError",
    "assess_org_identity_impact",
    "create_business_identity",
    "run_node_command",
    "STALE_VERSION_CODE",
    "STALE_VERSION_MESSAGE",
]
