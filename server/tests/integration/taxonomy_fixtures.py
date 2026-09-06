"""Fixture rows in the SUPERSEDED business taxonomy store, for pg-gated tests.

WHY THIS FILE EXISTS. On 2026-08-25 the four identity writers of
`core.business_taxonomy` stopped writing -- the acceptance schedule ratified in
`docs/product-architecture/governance.md`: an organization converges into Master
Data first, and writes there afterwards. Five pg-gated suites used those writers
as FIXTURES, to put a row in the store whose convergence, traversal or retirement
they then measured. That need did not go away with the door: a test of the
convergence has to be able to produce an unconverged row.

WHY IT IS NOT A SECOND WRITER. It mints nothing the product would mint. It is
plain SQL against a disposable database, in the tests tree, with the version row
written beside the identity exactly as migration 130's
`seed_business_domains_for_org` writes the six supplied domains -- a domain with
no version row is a shape no reader of this store ever sees. Nothing imports it
outside `server/tests/`.

The two version tables do NOT admit the same vocabulary, and the fixture obeys
each rather than picking one: migration 130 allows `seeded` on a DOMAIN version
and not on a CLASSIFICATION version, which never had a seeding path.

WHAT IT DELIBERATELY DOES NOT OFFER. No update, no archive. A test that needs an
ARCHIVED row asks for one at insert time (`status="archived"`), because the
product has no way to archive here any more and a helper that offered one would
be re-opening the door in the tests tree.
"""

from __future__ import annotations

import uuid
from typing import Any


def mint_taxonomy_id(prefix: str) -> str:
    """An id the CONVERGENCE will accept, which is not a free choice.

    Migration 307 constrains a governed node's id to
    `^(mdnode|bdm|bd|bcl)_([0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{26})$`, and the
    convergence carries a taxonomy row's OWN id onto its node. So a fixture that
    invented a readable id -- `bdm_probe_abc123` -- would insert happily and blow
    up 200 lines later inside the command under test, with a constraint name for
    a message. Minting here is what keeps that impossible: no caller chooses.
    """
    return f"{prefix}_{uuid.uuid4().hex[:26]}"

_DOMAIN_VERSION_SQL = """
INSERT INTO app.mdm_business_domain_versions
    (domain_id, version_number, org_id, slug, name, description, owner, status,
     created_by, created_at, updated_at, archived_at, change_kind, changed_by)
SELECT id, 1, org_id, slug, name, description, owner, status, created_by,
       created_at, updated_at, archived_at, 'seeded', %s
  FROM app.mdm_business_domains
 WHERE id = %s
"""

_CLASSIFICATION_VERSION_SQL = """
INSERT INTO app.mdm_business_classification_versions
    (classification_id, version_number, org_id, domain_id, parent_id,
     classification_type, slug, name, description, owner, status,
     created_by, created_at, updated_at, archived_at, change_kind, changed_by)
SELECT id, 1, org_id, domain_id, parent_id, classification_type, slug, name,
       description, owner, status, created_by, created_at, updated_at,
       archived_at, 'created', %s
  FROM app.mdm_business_classifications
 WHERE id = %s
"""

_DOMAIN_COLUMNS = (
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
)


def insert_domain_fixture(
    conn,
    *,
    org_id: str,
    slug: str,
    name: str,
    actor: str,
    domain_id: str | None = None,
    description: str = "",
    status: str = "active",
) -> dict[str, Any]:
    """One unconverged Business Domain, with its version row. Returns the row."""
    domain_id = domain_id or mint_taxonomy_id("bdm")
    archived_at = "now()" if status == "archived" else "NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.mdm_business_domains
                (id, org_id, slug, name, description, owner, status, created_by,
                 archived_at)
            VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, {archived_at})
            RETURNING {", ".join(_DOMAIN_COLUMNS)}
            """,  # noqa: S608 -- both interpolations are module-owned literals
            (domain_id, org_id, slug, name, description, status, actor),
        )
        row = dict(zip(_DOMAIN_COLUMNS, cur.fetchone(), strict=False))
        cur.execute(_DOMAIN_VERSION_SQL, (actor, domain_id))
    return row


def insert_classification_fixture(
    conn,
    *,
    org_id: str,
    domain_id: str,
    slug: str,
    name: str,
    actor: str,
    classification_id: str | None = None,
    parent_id: str | None = None,
    classification_type: str = "segment",
    description: str = "",
    status: str = "active",
) -> dict[str, Any]:
    """One unconverged classification, with its version row. Returns the row."""
    classification_id = classification_id or mint_taxonomy_id("bcl")
    columns = (
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
    )
    archived_at = "now()" if status == "archived" else "NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.mdm_business_classifications
                (id, org_id, domain_id, parent_id, classification_type, slug,
                 name, description, owner, status, created_by, archived_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, {archived_at})
            RETURNING {", ".join(columns)}
            """,  # noqa: S608 -- both interpolations are module-owned literals
            (
                classification_id,
                org_id,
                domain_id,
                parent_id,
                classification_type,
                slug,
                name,
                description,
                status,
                actor,
            ),
        )
        row = dict(zip(columns, cur.fetchone(), strict=False))
        cur.execute(_CLASSIFICATION_VERSION_SQL, (actor, classification_id))
    return row


__all__ = ["insert_classification_fixture", "insert_domain_fixture", "mint_taxonomy_id"]
