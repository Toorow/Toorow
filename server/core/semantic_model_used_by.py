"""A published Semantic Model version registers the Business Domains it names.

WHAT WAS OPEN, AND WHERE IT WAS WRITTEN. The cutover of 2026-08-25 moved every
Business Domain write into the Master Data authority and said, in the
*Incomplete if* of ``docs/product-architecture/governance.md``, what it took away
without putting back:

    a converged Business Domain can be archived while a published Semantic Model
    version still references it. The taxonomy writer refused that; the
    authority's impact guard reads ``app.master_data_used_by``, and no writer
    registers a Semantic Model version's Business Domain reference there yet.

``business_taxonomy.domain_used_by`` was kept for exactly this day and says so in
its own docstring -- *"it is a READ, and it is the reading that guard needs the
day it is registered on the authority's side"*. This module is that day. It does
not re-decide what a dependency means; it writes down, in the store the authority
already reads, the fact that reader computes by scanning two version tables.

WHY A WRITER AND NOT A SECOND READER. A guard could have joined
``semantic_view_versions`` and ``semantic_concept_versions`` from
``master_data_commands``. That is the bridge this module replaces, and it has two
defects the store does not: it makes the authority know the Semantic Model's
schema -- the enumeration of consumers migration 140 refuses in as many words
(*"enumerating consumers here would make the platform decide which surfaces are
allowed to depend on Master Data"*) -- and it answers only for the two surfaces
somebody remembered to join. A row in ``master_data_used_by`` is read by every
guard at once.

WHEN THE REFERENCE BECOMES TRUE, AND WHEN IT STOPS BEING TRUE.

* **Publication.** ``semantic_model._apply_concept`` and ``_apply_view`` write an
  immutable version with ``status = 'published'``. That is the single moment the
  reference starts holding, and the single place this module is called from --
  one writer, inside the confirm transaction, so a dependency cannot be lost
  while the version that declares it is live.
* **Supersession, not deletion.** The version that WAS published becomes
  ``superseded`` in the same statement; its ``used_by`` rows are RELEASED
  (``released_at``), never deleted. The order matters and it is the one
  ``governance_rule_sets`` already established: the new version registers FIRST
  and the superseded one is released after, both scoped by
  ``consumer_version_id``. Two consecutive versions routinely name the same
  domain, so releasing by consumer alone -- or releasing before registering --
  would mark the dependency that was just declared as gone.
* **Retirement.** ``_apply_archive`` retires the OBJECT. A retired Concept or
  View depends on nothing any more, so every live row of that consumer is
  released. This is what makes "withdraw the view, then the archive passes" a
  real repair path rather than a dead end.

A REFERENCE TO A DOMAIN THAT WAS NEVER CONVERGED IS NOT AN ERROR, AND IT IS NOT
SILENT EITHER. On 2026-08-25 production holds 12 Business Domains and 0 governed
nodes: until ``master_data_convergence`` runs, no reference has an authority to
be registered against, and there is nothing for an archive guard to protect --
the domain cannot be archived through Master Data, because Master Data does not
know it. Those references are counted and logged rather than dropped, so an
unwritten dependency is never invisible. A node that DOES exist and whose INSERT
fails is a different thing: it raises, the transaction rolls back, and the
publication is refused.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from core.business_taxonomy import LIVE_VERSION_STATUSES
from core.master_data import (
    UsedByReference,
    fetch_org_registry,
    register_used_by,
    release_used_by,
)
from core.master_data_convergence import DOMAIN_KIND

logger = logging.getLogger(__name__)

#: The two Semantic Model object types that may name a Business Domain. They are
#: the words `semantic_model` already uses for them (`_ARCHIVE_HEAD_TABLE`) and
#: the words `business_taxonomy.domain_used_by` already returns, so the store and
#: the reading it replaces speak of the same thing by the same name.
CONCEPT_KIND = "semantic-concept"
VIEW_KIND = "semantic-view"
CONSUMER_KINDS = (CONCEPT_KIND, VIEW_KIND)

#: Where each consumer's versions live, and which column names its object. Used
#: only by the backfill: the publication path is handed its own ids.
_VERSION_SOURCE: dict[str, tuple[str, str]] = {
    CONCEPT_KIND: ("app.semantic_concept_versions", "concept_id"),
    VIEW_KIND: ("app.semantic_view_versions", "view_id"),
}


def _clean_refs(refs: Any) -> list[str]:
    """The declared domain ids, de-duplicated, order preserved.

    ``_validate_business_domain_refs`` already refused a malformed list and a
    duplicate before the version was frozen. This is defensive rather than
    validating: a row written before that guard existed must not make a
    publication raise here.
    """

    if not isinstance(refs, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for ref in refs:
        value = str(ref or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _org_of_project(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"project {project_id!r} does not exist")
    return str(row[0])


def _governed_domain_nodes(
    conn, *, org_id: str, registry_id: str, domain_ids: Sequence[str]
) -> dict[str, str]:
    """The subset of ``domain_ids`` the authority actually owns, with its labels.

    Archived nodes are INCLUDED. An archived identity is exactly the one a
    restore has to reason about, and a row recording that a live version still
    names it is the truth; filtering here would make the restore path blind in
    the same way the archive path was.
    """

    if not domain_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label
              FROM app.master_data_nodes
             WHERE org_id = %s AND project_id IS NULL AND registry_id = %s
               AND id = ANY(%s)
            """,
            (org_id, registry_id, list(domain_ids)),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def register_published_version(
    conn,
    *,
    project_id: str,
    object_type: str,
    object_id: str,
    version_id: str,
    business_domain_refs: Any,
    superseded_version_ids: Sequence[str] = (),
    actor: str = "system",
) -> dict[str, Any]:
    """Record this version's Business Domain references, release the ones it replaces.

    Returns a small report -- ``registered``, ``unregistered``, ``released`` --
    so a caller can log what happened without re-reading the store. The counts
    are the point: ``unregistered`` is the number of references that named a
    domain the authority does not own yet, and a caller that prints it is a
    caller that cannot mistake a blind guard for a clear one.
    """

    if object_type not in CONSUMER_KINDS:
        raise ValueError(f"object_type must be one of {list(CONSUMER_KINDS)}")

    refs = _clean_refs(business_domain_refs)
    registered = 0
    unregistered: list[str] = []

    if refs:
        org_id = _org_of_project(conn, project_id)
        registry = fetch_org_registry(conn, org_id=org_id, object_kind=DOMAIN_KIND)
        if registry is None:
            unregistered = list(refs)
        else:
            nodes = _governed_domain_nodes(
                conn,
                org_id=org_id,
                registry_id=str(registry["id"]),
                domain_ids=refs,
            )
            for ref in refs:
                label = nodes.get(ref)
                if label is None:
                    unregistered.append(ref)
                    continue
                register_used_by(
                    conn,
                    project_id=project_id,
                    registry_id=str(registry["id"]),
                    reference=UsedByReference(
                        node_id=ref,
                        consumer_kind=object_type,
                        consumer_id=object_id,
                        consumer_label=label,
                        consumer_version_id=version_id,
                    ),
                    actor=actor,
                )
                registered += 1

    # AFTER the registrations, and scoped to the exact version replaced.
    released = 0
    for superseded in superseded_version_ids:
        value = str(superseded or "").strip()
        if not value or value == version_id:
            continue
        release_used_by(
            conn,
            project_id=project_id,
            consumer_kind=object_type,
            consumer_id=object_id,
            consumer_version_id=value,
        )
        released += 1

    if unregistered:
        logger.info(
            "semantic_model_used_by: %s %s version %s names %d Business Domain(s) with "
            "no governed node yet (%s); converge the organization to make them guarded",
            object_type,
            object_id,
            version_id,
            len(unregistered),
            ", ".join(unregistered),
        )

    return {
        "registered": registered,
        "unregistered": len(unregistered),
        "unregistered_refs": unregistered,
        "released": released,
    }


def release_object(conn, *, project_id: str, object_type: str, object_id: str) -> None:
    """A retired Concept or View depends on nothing. Every live row is released."""

    if object_type not in CONSUMER_KINDS:
        raise ValueError(f"object_type must be one of {list(CONSUMER_KINDS)}")
    release_used_by(
        conn,
        project_id=project_id,
        consumer_kind=object_type,
        consumer_id=object_id,
    )


# ---------------------------------------------------------------------------
# The backfill.
#
# The writer above records a reference the moment it becomes true. Every
# reference PUBLISHED BEFORE it existed is true and unrecorded, and so is every
# reference published before the domain it names was converged -- which, on
# 2026-08-25, is all of them: 12 Business Domains, 0 governed nodes.
#
# It is therefore not a one-shot script. It is the second half of the
# convergence: the moment `master_data_convergence._apply` mints the nodes is the
# moment the already-published references acquire an authority to be registered
# against, and the pass runs there, in the same transaction, under the same
# actor and the same audit row. Running it again registers the same rows onto
# themselves -- `register_used_by` upserts on
# (project, node, consumer_kind, consumer_id, consumer_version_id) -- so a replay
# is a no-op rather than a duplicate.
# ---------------------------------------------------------------------------


def backfill_org_references(conn, *, org_id: str, actor: str = "system") -> dict[str, Any]:
    """Register every live Semantic Model reference this organization already published.

    "Live" is ``business_taxonomy.LIVE_VERSION_STATUSES`` -- draft, candidate and
    published -- imported rather than retyped, because it is the definition the
    guard this replaces used. ``superseded`` and ``archived`` are history: they
    never come back, and blocking on them would make a domain unarchivable for
    ever.
    """

    registry = fetch_org_registry(conn, org_id=org_id, object_kind=DOMAIN_KIND)
    if registry is None:
        return {"registered": 0, "unregistered": 0, "versions": 0, "registry_id": None}

    union = " UNION ALL ".join(
        f"""
        SELECT '{kind}' AS consumer_kind, v.id AS version_id, v.{column} AS object_id,
               v.project_id, v.status,
               jsonb_array_elements_text(v.business_domain_refs) AS domain_id
          FROM {table} v
         WHERE v.project_id IN (SELECT id FROM app.projects WHERE org_id = %(org_id)s)
           AND jsonb_array_length(v.business_domain_refs) > 0
        """
        for kind, (table, column) in _VERSION_SOURCE.items()
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT consumer_kind, version_id, object_id, project_id, domain_id "
            f"FROM ({union}) refs WHERE status = ANY(%(live)s) "
            f"ORDER BY consumer_kind, object_id, version_id, domain_id",
            {"org_id": org_id, "live": list(LIVE_VERSION_STATUSES)},
        )
        rows = cur.fetchall()

    wanted = sorted({str(row[4]) for row in rows})
    nodes = _governed_domain_nodes(
        conn, org_id=org_id, registry_id=str(registry["id"]), domain_ids=wanted
    )

    registered = 0
    unregistered = 0
    for consumer_kind, version_id, object_id, project_id, domain_id in rows:
        label = nodes.get(str(domain_id))
        if label is None:
            unregistered += 1
            continue
        register_used_by(
            conn,
            project_id=str(project_id),
            registry_id=str(registry["id"]),
            reference=UsedByReference(
                node_id=str(domain_id),
                consumer_kind=str(consumer_kind),
                consumer_id=str(object_id),
                consumer_label=label,
                consumer_version_id=str(version_id),
            ),
            actor=actor,
        )
        registered += 1

    return {
        "registered": registered,
        "unregistered": unregistered,
        "versions": len({str(row[1]) for row in rows}),
        "registry_id": str(registry["id"]),
    }


__all__ = [
    "CONCEPT_KIND",
    "CONSUMER_KINDS",
    "VIEW_KIND",
    "backfill_org_references",
    "register_published_version",
    "release_object",
]
