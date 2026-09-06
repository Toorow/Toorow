"""The business taxonomy converges into the ONE authority, on a real database.

WHY THIS FILE IS pg-GATED AND NOT A MOCK SUITE. Every property the convergence
claims is a property of the DATABASE: the node keeps the taxonomy row's own id
because a CHECK constraint accepts `bd_`/`bcl_` (migration 143); a second run
mints nothing because the primary key would refuse it; a hard delete of a link is
impossible because a trigger says so (migration 306). A `MagicMock` cursor
answers whatever a fixture tells it to, and would prove none of the three.

WHAT IT WALKS, IN ORDER:

* the six domains an organization is seeded with reach the authority, keeping
  their ids, each with one published version;
* a classification converges AS A CLASSIFICATION -- the Implementation Gate's
  item 4 in one assertion -- carrying `classification_type` rather than being
  read as a Product or an Activity;
* the tree survives the move: the edge from a domain to its classification, and
  from a classification to its child, is a membership bound to the CHILD's
  version;
* running it again converges nothing and duplicates nothing, with a new
  idempotency key and with the same one, which are two different guarantees;
* an interrupted run is completed rather than duplicated;
* the source rows stay readable and name their successor and the operation.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.taxonomy_fixtures import (
    insert_classification_fixture,
    insert_domain_fixture,
)

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"


@pytest.fixture()
def scope(live_postgres):
    """One organization and one Project. The organization arrives with domains.

    `trg_organizations_seed_business_domains` (migration 130) writes the six
    starter Business Domains on INSERT, so this fixture does not seed them: the
    rows under test are the ones the product itself produces.
    """
    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, f"Org {suffix}", f"org-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, f"Project {suffix}", f"project-{suffix}", ACTOR),
        )
    live_postgres.commit()
    return {"suffix": suffix, "org_id": org_id, "project_id": project_id}


def _classification(conn, scope, *, slug: str, domain_id: str, parent_id=None, kind="segment"):
    """A row this store still holds and the authority does not -- written as a
    FIXTURE since 2026-08-25, because `create_classification` refuses now.

    The subject of this file is unchanged by that: converging needs an
    unconverged row to converge, and where it came from is not what is measured.
    """
    return insert_classification_fixture(
        conn,
        org_id=scope["org_id"],
        domain_id=domain_id,
        parent_id=parent_id,
        classification_type=kind,
        slug=slug,
        name=slug.replace("-", " ").title(),
        actor=ACTOR,
    )


def _a_domain(conn, scope) -> dict:
    from core import business_taxonomy

    taxonomy = business_taxonomy.list_taxonomy(conn, org_id=scope["org_id"])
    assert taxonomy["domains"], "the organization trigger seeds six domains"
    return taxonomy["domains"][0]


def _converge(conn, scope, *, key: str | None = None) -> dict:
    from core.master_data_convergence import converge_org_taxonomy

    return converge_org_taxonomy(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor=ACTOR,
        idempotency_key=key or f"conv_{uuid.uuid4().hex}",
        reason="AC1: one authority",
    )


# ---------------------------------------------------------------------------


def test_the_seeded_domains_reach_the_authority_keeping_their_own_identity(
    live_postgres, scope
):
    from core import master_data

    conn = live_postgres
    before = {
        str(row["id"]) for row in _all_domains(conn, scope)
    }
    assert len(before) == 6, "migration 130 seeds six starter domains"

    result = _converge(conn, scope)
    assert result["result"]["converged_domains"] == 6

    registry = master_data.require_org_registry(
        conn, org_id=scope["org_id"], object_kind="business_domain"
    )
    nodes = master_data.list_org_nodes(
        conn, org_id=scope["org_id"], registry_id=str(registry["id"])
    )
    # THE IDENTITY IS THE SAME OBJECT, not a copy with a new id. Every consumer
    # that pinned a Business Domain keeps resolving, which is the difference
    # between a convergence and a fork.
    assert {str(node["id"]) for node in nodes} == before
    assert {node["node_kind"] for node in nodes} == {"business_domain"}

    current = master_data.current_node_versions(
        conn, org_id=scope["org_id"], registry_id=str(registry["id"])
    )
    assert set(current) == before, "every identity has exactly one current version"


def test_a_classification_converges_as_a_classification_and_carries_its_type(
    live_postgres, scope
):
    """The Implementation Gate's item 4, in one test.

    *"migrate ambiguous business-classification rows as classifications, never
    guess that they are Products or Activities"*. So the node kind is
    `business_classification`, no `product` or `activity` registry is minted,
    and the word the organization chose survives in the payload instead of being
    read as a type.
    """
    from core import master_data

    conn = live_postgres
    domain = _a_domain(conn, scope)
    source = _classification(
        conn,
        scope,
        slug=f"paid-media-{scope['suffix']}",
        domain_id=str(domain["id"]),
        kind="product_line",
    )
    conn.commit()

    _converge(conn, scope)

    registry = master_data.require_org_registry(
        conn, org_id=scope["org_id"], object_kind="business_classification"
    )
    nodes = master_data.list_org_nodes(
        conn, org_id=scope["org_id"], registry_id=str(registry["id"])
    )
    assert len(nodes) == 1
    assert nodes[0]["node_kind"] == "business_classification"

    versions = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=str(nodes[0]["id"])
    )
    # Compared against the SOURCE row rather than against a literal: the writer
    # normalizes the word to a slug, and a hard-coded expectation would pin the
    # normalization instead of the property under test -- that whatever the
    # organization stored is what the authority carries.
    assert (
        versions[0]["payload"]["classification_type"] == source["classification_type"]
    )
    assert source["classification_type"] not in ("product", "activity")

    # No Product and no Activity registry was invented on the way.
    for guessed in ("product", "activity"):
        assert (
            master_data.fetch_org_registry(
                conn, org_id=scope["org_id"], object_kind=guessed
            )
            is None
        ), guessed


def test_the_tree_survives_the_move_as_edges_on_the_child_version(live_postgres, scope):
    from core import master_data

    conn = live_postgres
    domain = _a_domain(conn, scope)
    parent = _classification(
        conn, scope, slug=f"root-{scope['suffix']}", domain_id=str(domain["id"])
    )
    child = _classification(
        conn,
        scope,
        slug=f"leaf-{scope['suffix']}",
        domain_id=str(domain["id"]),
        parent_id=str(parent["id"]),
    )
    conn.commit()

    _converge(conn, scope)

    for node_id, expected_parent in (
        (str(parent["id"]), str(domain["id"])),
        (str(child["id"]), str(parent["id"])),
    ):
        versions = master_data.list_node_versions(
            conn, org_id=scope["org_id"], node_id=node_id
        )
        edges = master_data.fetch_org_memberships(
            conn, version_id=str(versions[0]["id"])
        )
        assert [
            (edge.parent_node_id, edge.child_node_id) for edge in edges
        ] == [(expected_parent, node_id)], node_id


def test_the_edge_is_inside_the_version_identity(live_postgres, scope):
    """A regroup changes the digest, or the optimistic lock is blind to it.

    `publish_node_version(expected_content_hash=...)` is what makes a review
    single-use. If an edge could move without moving the hash, a confirmation
    prepared before the move would still be accepted after it.
    """
    from core import master_data

    conn = live_postgres
    domain = _a_domain(conn, scope)
    node = _classification(
        conn, scope, slug=f"hashed-{scope['suffix']}", domain_id=str(domain["id"])
    )
    conn.commit()
    _converge(conn, scope)

    registry = master_data.require_org_registry(
        conn, org_id=scope["org_id"], object_kind="business_classification"
    )
    published = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=str(node["id"])
    )[0]

    unedged = master_data.node_version_digest(
        node_id=str(node["id"]),
        payload=published["payload"],
        type_version_id=published["type_version_id"],
        vocabulary_version_id=published["vocabulary_version_id"],
    )
    assert published["content_hash"] != unedged
    assert registry["version_scope"] == "node"


def test_a_second_run_converges_nothing_and_duplicates_nothing(live_postgres, scope):
    """Idempotence BELOW the idempotency key.

    A retry after a client timeout arrives with a new key. If the command relied
    on the key alone it would mint a second identity for every domain -- which
    the primary key would refuse, turning a retry into a permanent failure.
    """
    conn = live_postgres
    domain = _a_domain(conn, scope)
    _classification(
        conn, scope, slug=f"twice-{scope['suffix']}", domain_id=str(domain["id"])
    )
    conn.commit()

    first = _converge(conn, scope)
    assert first["result"]["converged_domains"] == 6
    assert first["result"]["converged_classifications"] == 1

    second = _converge(conn, scope)  # a DIFFERENT key
    assert second["idempotent_replay"] is False
    assert second["result"]["converged_domains"] == 0
    assert second["result"]["converged_classifications"] == 0
    assert second["result"]["plan"]["already_converged_domains"] == 6

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.master_data_nodes WHERE org_id = %s",
            (scope["org_id"],),
        )
        assert cur.fetchone()[0] == 7


def test_the_same_idempotency_key_returns_the_first_result_without_acting_again(
    live_postgres, scope
):
    conn = live_postgres
    key = f"conv_{uuid.uuid4().hex}"

    first = _converge(conn, scope, key=key)
    replay = _converge(conn, scope, key=key)

    assert replay["idempotent_replay"] is True
    assert replay["operation_id"] == first["operation_id"]
    assert replay["result"]["converged_domains"] == first["result"]["converged_domains"]


def test_an_interrupted_run_is_completed_rather_than_duplicated(live_postgres, scope):
    """The NODE's existence is the fact checked, never the stamp alone.

    A run that created the node and died before stamping its source row leaves a
    state that a stamp-only check would try to converge again -- and the primary
    key would refuse it forever. This unstamps a converged row on purpose and
    proves the second pass finishes it.
    """
    from core import master_data

    conn = live_postgres
    _converge(conn, scope)
    domain = _all_domains(conn, scope)[0]

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_business_domains "
            "SET superseded_at = NULL, superseded_by_node_id = NULL, "
            "    superseded_by_operation_id = NULL "
            "WHERE id = %s",
            (str(domain["id"]),),
        )

    result = _converge(conn, scope)
    assert result["result"]["converged_domains"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT superseded_by_node_id FROM app.mdm_business_domains WHERE id = %s",
            (str(domain["id"]),),
        )
        assert cur.fetchone()[0] == str(domain["id"])
    assert (
        master_data.fetch_org_node(
            conn, org_id=scope["org_id"], node_id=str(domain["id"])
        )
        is not None
    )


def test_the_source_row_stays_readable_and_names_who_moved_it(live_postgres, scope):
    conn = live_postgres
    result = _converge(conn, scope)
    operation_id = result["operation_id"]

    rows = _all_domains(conn, scope)
    assert len(rows) == 6, "no source row is destroyed"
    for row in rows:
        assert row["superseded_at"] is not None
        assert row["superseded_by_node_id"] == str(row["id"])
        assert row["superseded_by_operation_id"] == operation_id

    with conn.cursor() as cur:
        cur.execute(
            "SELECT command_type FROM app.operations WHERE id = %s", (operation_id,)
        )
        assert cur.fetchone()[0] == "governance.master_data.converge_business_taxonomy"


def test_an_archived_domain_converges_archived_with_its_history_published(
    live_postgres, scope
):
    """Archived in the source is archived in the authority -- and still readable.

    Skipping archived rows would narrow the authority to the live half of the
    taxonomy, and a reader opening a domain archived last year would find no
    governed object at all, which reads as "it never existed".
    """
    from core import master_data

    conn = live_postgres
    # An archived row, written archived. `update_domain` refuses since
    # 2026-08-25, and the fixture helper offers no archive on purpose: the
    # product has no way to archive here any more, so a helper that offered one
    # would re-open the door inside the tests tree.
    domain = insert_domain_fixture(
        conn,
        org_id=scope["org_id"],
        slug=f"archived-{scope['suffix']}",
        name="Archived last year",
        actor=ACTOR,
        status="archived",
    )
    conn.commit()

    _converge(conn, scope)

    node = master_data.fetch_org_node(
        conn, org_id=scope["org_id"], node_id=str(domain["id"])
    )
    assert node is not None
    assert node["archived_at"] is not None
    versions = master_data.list_node_versions(
        conn, org_id=scope["org_id"], node_id=str(domain["id"])
    )
    assert versions and versions[0]["status"] == "current"
    assert versions[0]["payload"]["lifecycle_state"] == "archived"


def test_a_classification_naming_an_unreachable_parent_is_refused_not_dropped(
    live_postgres, scope
):
    """A branch that cannot be placed stops the command; it is never skipped.

    Silently dropping it would leave an authority that is missing a subtree and
    a report saying everything converged.
    """
    from core.master_data_convergence import ConvergenceRefused

    conn = live_postgres
    domain = _a_domain(conn, scope)
    orphan = _classification(
        conn, scope, slug=f"orphan-{scope['suffix']}", domain_id=str(domain["id"])
    )
    conn.commit()
    # A parent outside the organization's taxonomy. The foreign key on
    # `parent_id` makes this unreachable through the product's own writers,
    # which is why it is forced here: the refusal must exist before the shape
    # can appear, not after.
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE app.mdm_business_classifications "
            "DROP CONSTRAINT fk_mdm_classification_parent"
        )
        cur.execute(
            "UPDATE app.mdm_business_classifications SET parent_id = %s WHERE id = %s",
            ("bcl_00000000000000000000000000", str(orphan["id"])),
        )
    try:
        with pytest.raises(ConvergenceRefused) as refused:
            _converge(conn, scope)
        assert str(orphan["id"]) in refused.value.detail["unreachable_parents"]
    finally:
        conn.rollback()


def _all_domains(conn, scope) -> list[dict]:
    fields = (
        "id",
        "slug",
        "status",
        "superseded_at",
        "superseded_by_node_id",
        "superseded_by_operation_id",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(fields)} FROM app.mdm_business_domains "  # noqa: S608
            "WHERE org_id = %s ORDER BY slug",
            (scope["org_id"],),
        )
        return [dict(zip(fields, row, strict=False)) for row in cur.fetchall()]
