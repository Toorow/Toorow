"""Which Business Domain / classification an id IS -- the authority first (story 49.2).

WHY THIS MODULE EXISTS. `app.mdm_business_domains` and
`app.mdm_business_classifications` are the SUPERSEDED business taxonomy store.
Since the cutover of 2026-08-25 their four write doors answer 409
`legacy_store_is_read_only` (`business_taxonomy.LEGACY_TAXONOMY_WRITE_REFUSED_
MESSAGE`), the authority is `app.master_data_nodes` /
`app.master_data_object_versions`, and the creation gesture writes a row into
the old store BORN SUPERSEDED so the fourteen readers keep working
(`governance.md` § *Decision 2 — the superseded store keeps a READ projection,
written only by the authority*). That decision names its own remainder in one
sentence: the readers are *"re-pointed reader by reader, never by a big-bang"*.
This module is what they are re-pointed AT.

THE ORDER, AND IT IS ONE-DIRECTIONAL. The authority answers FIRST; the
superseded row answers only for an id the authority holds no node for -- the
organization that has not converged yet. Never the inverse. The rule is the one
`governed_field_catalogue` already runs under for `app.target_fields`, and it is
the only order that can add an identity without ever removing one: an id the
authority does not govern makes this module ABSTAIN (it falls through) rather
than answer "no such object".

THE IDENTITY IS THE SAME OBJECT ON BOTH SIDES, which is why one union can serve
both. Migration 143 widened `master_data_nodes_id_check` to accept a `bd_` /
`bcl_` id precisely so a convergence keeps the id its consumers already pinned
(`master_data.create_org_node`, and `master_data_convergence` §2
*Identity-preserving*). So a `bd_…` names ONE business object, whichever store
is answering for it -- and `NOT EXISTS (… master_data_nodes …)` on the
superseded half is enough to keep a converged identity from being read twice.

WHAT THE PAYLOAD SAYS OUT LOUD. Every row carries `source` -- `authority` or
`superseded`. A caller never infers which store answered from the shape of what
it got back; a facet that means "the authority holds this" must be able to say
so without guessing.

WHAT IT NEVER DOES. It does not invent. An id NEITHER store carries is absent
from the answer, and the caller keeps the honest empty state it already had. It
writes nothing, opens no connection of its own, and reads `app.mdm_business_*`
in exactly one place -- this file -- which is what
`tests/core/test_business_domain_readers_are_declared.py` ratchets.

TWO WAYS IN, BECAUSE THE READERS ARE TWO SHAPES. Nine of the fourteen readers
JOIN the store inside a larger statement (a lens with four LATERALs, a link
walk, a picker); rewriting those in Python would move a filter, an ORDER BY and
a LEFT JOIN's null semantics all at once, which is how "nothing changes for a
person" stops being true. They interpolate the SQL sources below -- module-owned
literals, never a caller's string -- and keep their own statement. The rest read
rows and get the Python helpers.
"""

from __future__ import annotations

import logging
from typing import Any

from core.master_data_convergence import CLASSIFICATION_KIND, DOMAIN_KIND

logger = logging.getLogger(__name__)

#: Where an answer came from. `authority` is `app.master_data_nodes`;
#: `superseded` is the legacy row of an organization that has not converged.
AUTHORITY = "authority"
SUPERSEDED = "superseded"

#: The columns a domain row answers with -- the superseded store's OWN column
#: names, plus `source`. The fourteen readers were written against those names,
#: and translating once here is what lets them move one at a time instead of
#: being rewritten together.
DOMAIN_COLUMNS = (
    "id",
    "org_id",
    "slug",
    "name",
    "description",
    "owner",
    "status",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
    "source",
)

CLASSIFICATION_COLUMNS = (
    "id",
    "org_id",
    "domain_id",
    "parent_id",
    "classification_type",
    "slug",
    "name",
    "description",
    "owner",
    "status",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
    "source",
)

VERSION_COLUMNS = ("org_id", "version_number", "changed_at", "changed_by", "source")

# ---------------------------------------------------------------------------
# The SQL sources. Each one is a parenthesised sub-select with EXACTLY the
# columns of the table it replaces, so a reader substitutes it for
# `app.mdm_business_domains` and changes nothing else -- its alias, its WHERE,
# its ORDER BY and its LEFT JOIN nullability all keep meaning what they meant.
#
# `status` is DERIVED on the authority half rather than read: a node's lifecycle
# IS `archived_at`, and storing the word beside it would give one object two
# lifecycles that can disagree. `name` prefers the published payload and falls
# back to the node label, which is the same value by construction -- a rename
# republishes both in one transaction (`master_data_commands`) -- but a node
# whose only version is still a draft has a label and no payload, and answering
# NULL there would blank a name that plainly exists.
# ---------------------------------------------------------------------------

_AUTHORITY_NODE_EXISTS = """
    SELECT 1
      FROM app.master_data_nodes n
     WHERE n.id = {id_expression}
       AND n.org_id = {org_expression}
       AND n.project_id IS NULL
"""

#: THE VERSION GUARD IS NOT THE IDENTITY GUARD, and story 49.2's review round 1
#: is why this is spelled out here. On the two IDENTITY sources the fallback is
#: keyed on the node: one node, one identity, so a node's existence is exactly
#: the fact that decides who answers. On the two VERSION sources that same key
#: DESTROYS HISTORY, because a convergence mints exactly ONE node version
#: (`master_data_convergence._converge_row` -> a single `create_node_version`
#: carrying the row's current payload) and imports none of the ledger behind it.
#: Measured on a domain carrying legacy revisions 1, 2 and 3: keyed on the node,
#: converging it made the Versions facet drop from three to one, `list_taxonomy`
#: report `version_number = 1`, and every AI path pinned at version 3 read as
#: drifted -- a convergence is not supposed to be able to lose a revision.
#:
#: So the version fallback is keyed per `(id, version_number)`: the legacy ledger
#: contributes every revision NUMBER the authority does not hold, and the
#: authority answers wherever both speak.
#:
#: A COLLISION IS NOW A DEFECT, NOT A DESIGN (AI-324, decided by Jean on
#: 2026-08-31). The numbering used to collide ON PURPOSE -- the authority
#: restarted at 1 for a converged node -- so one NUMBER could designate two
#: different CONTENTS and this clause silently chose one of them. The writer
#: mints above the union's maximum now (`master_data._NEXT_NODE_VERSION_NUMBER`)
#: and migration 327 renumbered the rows that already collided, so this clause
#: keeps the same behaviour for exactly one reason: it must remain impossible for
#: a superseded row to mask the governed one while the defect is being fixed
#: somewhere it cannot reach. The Python half below SHOUTS when it fires --
#: `_one_content_per_number`.
_AUTHORITY_REVISION_EXISTS = """
    SELECT 1
      FROM app.master_data_object_versions held
      JOIN app.master_data_nodes held_node
        ON held_node.id = held.node_id
       AND held_node.org_id = held.org_id
       AND held_node.project_id IS NULL
     WHERE held.node_id = {id_expression}
       AND held.org_id = {org_expression}
       AND held.version_number = {number_expression}
       AND held.published_at IS NOT NULL
"""

DOMAIN_SOURCE = f"""(
    SELECT n.id,
           n.org_id,
           mdv.payload->>'slug' AS slug,
           COALESCE(mdv.payload->>'name', n.label) AS name,
           COALESCE(mdv.payload->>'description', '') AS description,
           mdv.payload->>'owner' AS owner,
           CASE WHEN n.archived_at IS NULL THEN 'active' ELSE 'archived' END AS status,
           n.created_by,
           n.created_at,
           n.updated_at,
           n.archived_at,
           '{AUTHORITY}'::TEXT AS source
      FROM app.master_data_nodes n
      LEFT JOIN app.master_data_object_versions mdv
             ON mdv.node_id = n.id AND mdv.org_id = n.org_id AND mdv.status = 'current'
     WHERE n.project_id IS NULL AND n.node_kind = '{DOMAIN_KIND}'
    UNION ALL
    SELECT legacy.id,
           legacy.org_id,
           legacy.slug,
           legacy.name,
           legacy.description,
           legacy.owner,
           legacy.status,
           legacy.created_by,
           legacy.created_at,
           legacy.updated_at,
           legacy.archived_at,
           '{SUPERSEDED}'::TEXT AS source
      FROM app.mdm_business_domains legacy
     WHERE NOT EXISTS ({
    _AUTHORITY_NODE_EXISTS.format(id_expression="legacy.id", org_expression="legacy.org_id")
})
)"""

CLASSIFICATION_SOURCE = f"""(
    SELECT n.id,
           n.org_id,
           mdv.payload->>'domain_node_id' AS domain_id,
           mdv.payload->>'parent_node_id' AS parent_id,
           mdv.payload->>'classification_type' AS classification_type,
           mdv.payload->>'slug' AS slug,
           COALESCE(mdv.payload->>'name', n.label) AS name,
           COALESCE(mdv.payload->>'description', '') AS description,
           mdv.payload->>'owner' AS owner,
           CASE WHEN n.archived_at IS NULL THEN 'active' ELSE 'archived' END AS status,
           n.created_by,
           n.created_at,
           n.updated_at,
           n.archived_at,
           '{AUTHORITY}'::TEXT AS source
      FROM app.master_data_nodes n
      LEFT JOIN app.master_data_object_versions mdv
             ON mdv.node_id = n.id AND mdv.org_id = n.org_id AND mdv.status = 'current'
     WHERE n.project_id IS NULL AND n.node_kind = '{CLASSIFICATION_KIND}'
    UNION ALL
    SELECT legacy.id,
           legacy.org_id,
           legacy.domain_id,
           legacy.parent_id,
           legacy.classification_type,
           legacy.slug,
           legacy.name,
           legacy.description,
           legacy.owner,
           legacy.status,
           legacy.created_by,
           legacy.created_at,
           legacy.updated_at,
           legacy.archived_at,
           '{SUPERSEDED}'::TEXT AS source
      FROM app.mdm_business_classifications legacy
     WHERE NOT EXISTS ({
    _AUTHORITY_NODE_EXISTS.format(id_expression="legacy.id", org_expression="legacy.org_id")
})
)"""

# A PUBLISHED revision, never a draft. `master_data.publish_node_version` is what
# stamps `published_at`, and the two statuses that carry one are `current` and
# `superseded` -- the same history the superseded store's append-only ledger
# holds. Counting drafts would put a revision nobody approved into the Versions
# facet of a Governance lens, which is the one place a person reads approval.
#: THE BASE A RENAME IS PRECONDITIONED ON, written once so no reader derives its
#: own. `governance.md` (amendment of 2026-08-30) requires that a door renaming an
#: identity send back the version identity IT READ, and that only works while
#: every door reads the same thing: the id of the authority's CURRENT published
#: revision, or NULL where the object has none yet.
#:
#: It is deliberately NOT `version_number`. The two version sources above union
#: two ledgers that number the same identity independently -- that union is what
#: keeps a convergence from losing a revision -- so the number a reader holds
#: cannot be compared with anything. An id can: it is minted once and never
#: renumbered.
CURRENT_VERSION_ID = """
    (SELECT held.id
       FROM app.master_data_object_versions held
      WHERE held.node_id = {id_expression}
        AND held.org_id = {org_expression}
        AND held.status = 'current')
"""

def _version_source(
    *, node_kind: str, legacy_table: str, legacy_id_column: str, suppressed: bool
) -> str:
    """One version source, with or without the anti-masking clause.

    TWO VARIANTS OF ONE STATEMENT, WRITTEN ONCE. The SUPPRESSED variant is what
    the nine readers that interpolate this module's SQL into their own statement
    get: a legacy revision whose number the authority also holds is dropped, so a
    superseded row can never mask the governed one. The UNSUPPRESSED variant is
    read only by the Python half of this module, which needs to SEE the collision
    in order to shout about it (`_one_content_per_number`) -- a `NOT EXISTS` that
    hides the defect from the only code that could report it is how AI-324 stayed
    invisible for three days.
    """

    anti_masking = (
        _AUTHORITY_REVISION_EXISTS.format(
            id_expression=f"legacy.{legacy_id_column}",
            org_expression="legacy.org_id",
            number_expression="legacy.version_number",
        )
        if suppressed
        else " SELECT 1 WHERE FALSE "
    )
    return f"""(
    SELECT mdv.node_id AS {legacy_id_column},
           mdv.org_id,
           mdv.version_number,
           mdv.published_at AS changed_at,
           mdv.published_by AS changed_by,
           '{AUTHORITY}'::TEXT AS source
      FROM app.master_data_object_versions mdv
      JOIN app.master_data_nodes n
        ON n.id = mdv.node_id AND n.org_id = mdv.org_id AND n.project_id IS NULL
     WHERE n.node_kind = '{node_kind}' AND mdv.published_at IS NOT NULL
    UNION ALL
    SELECT legacy.{legacy_id_column},
           legacy.org_id,
           legacy.version_number,
           legacy.changed_at,
           legacy.changed_by,
           '{SUPERSEDED}'::TEXT AS source
      FROM {legacy_table} legacy
     WHERE NOT EXISTS ({anti_masking})
)"""


DOMAIN_VERSION_SOURCE = _version_source(
    node_kind=DOMAIN_KIND,
    legacy_table="app.mdm_business_domain_versions",
    legacy_id_column="domain_id",
    suppressed=True,
)

CLASSIFICATION_VERSION_SOURCE = _version_source(
    node_kind=CLASSIFICATION_KIND,
    legacy_table="app.mdm_business_classification_versions",
    legacy_id_column="classification_id",
    suppressed=True,
)

#: The same two unions with nothing hidden. Module-private on purpose: a reader
#: that took these would read one identity's revision twice.
_DOMAIN_VERSION_SOURCE_UNSUPPRESSED = _version_source(
    node_kind=DOMAIN_KIND,
    legacy_table="app.mdm_business_domain_versions",
    legacy_id_column="domain_id",
    suppressed=False,
)

_CLASSIFICATION_VERSION_SOURCE_UNSUPPRESSED = _version_source(
    node_kind=CLASSIFICATION_KIND,
    legacy_table="app.mdm_business_classification_versions",
    legacy_id_column="classification_id",
    suppressed=False,
)


# ---------------------------------------------------------------------------
# The Python half, for the readers that fetch rows rather than join.
# ---------------------------------------------------------------------------


def _fetch(conn: Any, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def _identities(
    conn: Any,
    source: str,
    columns: tuple[str, ...],
    *,
    org_id: str,
    ids: list[str] | None,
    status: str,
) -> dict[str, dict[str, Any]]:
    if ids is not None and not ids:
        return {}
    # `all_ids` is a BOOLEAN parameter rather than a `%s IS NULL` guard: an
    # untyped parameter compared with IS NULL makes Postgres refuse the whole
    # statement, and `test_sql_parameter_typing` exists because that class came
    # back eight times.
    query = f"""
        SELECT {", ".join(columns)}
          FROM {source} identity
         WHERE identity.org_id = %s
           AND (%s OR identity.status = %s)
           AND (%s OR identity.id = ANY(%s))
         ORDER BY identity.name, identity.id
    """
    rows = _fetch(
        conn,
        query,
        (org_id, status == "all", status, ids is None, list(ids or [])),
    )
    return {str(row["id"]): row for row in rows}


def _checked_status(status: str) -> str:
    if status not in ("active", "archived", "all"):
        raise ValueError("status must be one of: active, archived, all")
    return status


def resolve_domains(
    conn: Any,
    *,
    org_id: str,
    ids: list[str] | None = None,
    status: str = "all",
) -> dict[str, dict[str, Any]]:
    """`{id -> Business Domain}` for this organization, authority first.

    ``ids=None`` asks for all of them. An id neither store carries is simply
    absent: the caller renders the empty state it already had rather than being
    handed an invented label.
    """
    return _identities(
        conn,
        DOMAIN_SOURCE,
        DOMAIN_COLUMNS,
        org_id=org_id,
        ids=ids,
        status=_checked_status(status),
    )


def resolve_classifications(
    conn: Any,
    *,
    org_id: str,
    ids: list[str] | None = None,
    status: str = "all",
) -> dict[str, dict[str, Any]]:
    """`{id -> Business Classification}` for this organization, authority first."""
    return _identities(
        conn,
        CLASSIFICATION_SOURCE,
        CLASSIFICATION_COLUMNS,
        org_id=org_id,
        ids=ids,
        status=_checked_status(status),
    )


def resolve_domain(conn: Any, *, org_id: str, domain_id: str) -> dict[str, Any] | None:
    """One Business Domain, or None. None means NEITHER store holds this id."""
    return resolve_domains(conn, org_id=org_id, ids=[str(domain_id)]).get(str(domain_id))


def resolve_classification(
    conn: Any, *, org_id: str, classification_id: str
) -> dict[str, Any] | None:
    """One Business Classification, or None."""
    key = str(classification_id)
    return resolve_classifications(conn, org_id=org_id, ids=[key]).get(key)


def domain_by_slug(
    conn: Any, *, org_id: str, slug: str, status: str = "active"
) -> dict[str, Any] | None:
    """The Business Domain this organization calls *slug*, authority first.

    A natively created identity carries its slug in the published payload, so it
    is findable here the day it is minted -- which is what a seed pass needs in
    order to link a connector's platform card to a domain nobody converged.
    """
    matches = [
        row
        for row in resolve_domains(conn, org_id=org_id, status=_checked_status(status)).values()
        if row.get("slug") == slug
    ]
    return matches[0] if matches else None


#: What the log says when one number designates two contents. A code rather than
#: a sentence so an operator can grep for it across every service at once.
VERSION_NUMBER_COLLISION_CODE = "version_number_designates_two_contents"


def _one_content_per_number(
    rows: list[dict[str, Any]], *, org_id: str, identity_id: str, what: str
) -> list[dict[str, Any]]:
    """Keep one revision per number, and SHOUT if there was ever a choice to make.

    THIS IS AN ASSERTION PATH, NOT A MERGE RULE (AI-324). Since the writer mints
    above the union's maximum and migration 327 renumbered what already collided,
    two rows at one number mean a version was minted below the legacy maximum --
    a defect in the writer or a row written around it, never a normal state.

    IT LOGS, IT DOES NOT RAISE, and the reason is what calls it. Every caller is
    a READ door: the Governance lens' Versions facet, `list_taxonomy`'s
    `version_number`, and the pin validators of Golden Questions and feedback
    aggregates. Raising would blank a screen -- or refuse a pin the person is
    allowed to make -- over a row they did not write and have no gesture to
    repair; the honest empty state is not empty here, it is the authority's own
    history, which is exactly what the union exists to keep. And nine readers
    interpolate the SQL source into their own statement, where no Python
    assertion runs at all: a raise here would make the two halves of one resolver
    disagree about whether the same data is legal. So the screen keeps answering
    with the governed revision, and the defect is shouted at the operator, who is
    the only one who can renumber.
    """

    by_number: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_number.setdefault(int(row["version_number"]), []).append(row)

    resolved: list[dict[str, Any]] = []
    for number in sorted(by_number, reverse=True):
        group = by_number[number]
        if len(group) == 1:
            resolved.append(group[0])
            continue
        authority = [row for row in group if row["source"] == AUTHORITY]
        logger.error(
            "%s: %s version %s of %s (org %s) designates %d contents across "
            "%s. The governed revision answers; the others are unreadable "
            "through this door. Renumber the authority revisions of this "
            "identity above the superseded ledger's maximum -- that is what "
            "%s mints and migration 327 repaired.",
            VERSION_NUMBER_COLLISION_CODE,
            what,
            number,
            identity_id,
            org_id,
            len(group),
            sorted({str(row["source"]) for row in group}),
            "master_data._NEXT_NODE_VERSION_NUMBER",
        )
        resolved.extend(authority or group[:1])
    return resolved


def domain_versions(conn: Any, *, org_id: str, domain_id: str) -> list[dict[str, Any]]:
    """The published revisions of one Business Domain, newest first.

    This is the read `governance.md` names as the debt Decision 2 leaves open:
    *"a natively created identity reads `None` there [the Versions facet] until
    the lens is re-pointed"*. The authority's own version ledger answers for a
    node it holds; the legacy ledger answers only where no node exists.

    It reads the UNSUPPRESSED union and resolves the overlap in Python, so a
    number that designates two contents is shouted about rather than quietly
    halved (`_one_content_per_number`). The rows it returns are the same rows the
    suppressed SQL source returns -- the choice is identical, only now it is
    reported.
    """
    rows = _fetch(
        conn,
        f"""
        SELECT {", ".join(VERSION_COLUMNS)}
          FROM {_DOMAIN_VERSION_SOURCE_UNSUPPRESSED} versions
         WHERE versions.org_id = %s AND versions.domain_id = %s
         ORDER BY versions.version_number DESC
        """,
        (org_id, str(domain_id)),
    )
    return _one_content_per_number(
        rows, org_id=org_id, identity_id=str(domain_id), what="Business Domain"
    )


def classification_versions(
    conn: Any, *, org_id: str, classification_id: str
) -> list[dict[str, Any]]:
    """The published revisions of one Business Classification, newest first."""
    rows = _fetch(
        conn,
        f"""
        SELECT {", ".join(VERSION_COLUMNS)}
          FROM {_CLASSIFICATION_VERSION_SOURCE_UNSUPPRESSED} versions
         WHERE versions.org_id = %s AND versions.classification_id = %s
         ORDER BY versions.version_number DESC
        """,
        (org_id, str(classification_id)),
    )
    return _one_content_per_number(
        rows,
        org_id=org_id,
        identity_id=str(classification_id),
        what="Business Classification",
    )


def domain_version_exists(
    conn: Any, *, org_id: str, domain_id: str, version_number: int
) -> bool:
    """Does this organization hold that revision of that Business Domain?

    The pin validators' question, and the reason they are re-pointed: a Golden
    Question or a feedback aggregate pins `(domain_id, version_number)`, and a
    natively created identity has version 1 in the authority and NO row at all in
    the legacy ledger. Asking the old store would refuse a pin on an identity the
    product just minted.
    """
    return any(
        int(row["version_number"]) == int(version_number)
        for row in domain_versions(conn, org_id=org_id, domain_id=domain_id)
    )


__all__ = [
    "AUTHORITY",
    "CLASSIFICATION_COLUMNS",
    "CLASSIFICATION_SOURCE",
    "CLASSIFICATION_VERSION_SOURCE",
    "DOMAIN_COLUMNS",
    "DOMAIN_SOURCE",
    "DOMAIN_VERSION_SOURCE",
    "SUPERSEDED",
    "VERSION_COLUMNS",
    "VERSION_NUMBER_COLLISION_CODE",
    "classification_versions",
    "domain_by_slug",
    "domain_version_exists",
    "domain_versions",
    "resolve_classification",
    "resolve_classifications",
    "resolve_domain",
    "resolve_domains",
]
