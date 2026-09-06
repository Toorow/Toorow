"""Used by NAMES its consumers, and Versions names its versions (49.2 AC7/AC10).

THE PROBE THIS FILE REPLAYS. The story verdict of 2026-09-01 opened a Business
Domain with three live rows in ``app.mdm_business_links`` and read back::

    {"state": "available", "count": 3, "refs": [], "truncated": false}

which ``GovernanceObjectWorkbench`` rendered as *"Nothing depends on this
object. Its owner answered, and the answer is none."* -- in the tab a person
opens before archiving. The same shape served ``versions`` for a Product with
three published revisions, and the screen said *"No version recorded"*.

WHY IT NEEDS A REAL DATABASE. Every property here is a property of the query,
not of the Python: the three consumer stores are joined by the read model, the
label of each target comes from the owner's own table through a LEFT JOIN, and
the node-scoped version ledger is written by a trigger-guarded insert. A cursor
that answers whatever the fixture says would prove the fixture.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"


@pytest.fixture()
def domain_with_three_consumers(live_postgres):
    """One organization, one Project, one Business Domain, three live links."""
    from core import business_taxonomy

    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    topic_id = f"top_{suffix}"
    procedure_id = f"prc_{suffix}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, f"Org {suffix}", f"org-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, f"Project {suffix}", f"project-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.context_topics (id, project_id, title, body_md, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (topic_id, project_id, f"Topic {suffix}", "body", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.procedures "
            "(id, project_id, name, frontmatter_yaml, body_md, created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (procedure_id, project_id, f"procedure-{suffix}", "{}", "body", ACTOR),
        )
        # A Data-owned target, so the three consumers do not all land in one
        # workspace: a grouping that only ever sees one group proves nothing.
        cur.execute(
            "SELECT name FROM app.target_fields WHERE status <> 'deleted' ORDER BY name LIMIT 1"
        )
        field = cur.fetchone()
    live_postgres.commit()
    assert field is not None, "the platform target-field catalogue is empty"

    domain = business_taxonomy.list_taxonomy(live_postgres, org_id=org_id)["domains"][0]
    for target_type, target_id in (
        ("topic", topic_id),
        ("procedure", procedure_id),
        ("target_field", str(field[0])),
    ):
        business_taxonomy.create_link(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            taxonomy_type="business_domain",
            taxonomy_id=str(domain["id"]),
            target_type=target_type,
            target_id=target_id,
            relation_type="explains",
            actor=ACTOR,
            reason="used-by proof",
        )
    live_postgres.commit()
    return {
        "org_id": org_id,
        "project_id": project_id,
        "domain_id": str(domain["id"]),
        "topic_id": topic_id,
        "procedure_id": procedure_id,
        "field_name": str(field[0]),
    }


def _compose(conn, fixture, object_type, object_id):
    from core.governance_read_model import compose_governance_object

    return compose_governance_object(
        fixture["project_id"],
        "master-data",
        object_type,
        object_id,
        conn,
        org_id=fixture["org_id"],
    )["object"]


# ---------------------------------------------------------------------------
# Used by
# ---------------------------------------------------------------------------


def test_three_live_links_are_three_named_refs_with_their_workspaces(
    live_postgres, domain_with_three_consumers
):
    """The exact probe of the verdict, and the exact answer it owed."""
    detail = _compose(
        live_postgres,
        domain_with_three_consumers,
        "business-domain",
        domain_with_three_consumers["domain_id"],
    )
    facet = detail["used_by"]

    assert facet["state"] == "available"
    assert facet["count"] == 3
    assert len(facet["refs"]) == 3

    by_id = {ref["id"]: ref for ref in facet["refs"]}
    assert set(by_id) == {
        domain_with_three_consumers["topic_id"],
        domain_with_three_consumers["procedure_id"],
        domain_with_three_consumers["field_name"],
    }
    # Each one says WHICH workspace owns it. A used-by that cannot name the
    # owner sends a person hunting through six workspaces for three rows.
    assert by_id[domain_with_three_consumers["topic_id"]]["workspace"] == "context-hub"
    assert by_id[domain_with_three_consumers["procedure_id"]]["workspace"] == "context-hub"
    assert by_id[domain_with_three_consumers["field_name"]]["workspace"] == "data"


def test_each_ref_carries_a_label_an_evidence_time_and_an_exact_owner_link(
    live_postgres, domain_with_three_consumers
):
    """AC7 verbatim, minus the pin: a business link pins no version."""
    detail = _compose(
        live_postgres,
        domain_with_three_consumers,
        "business-domain",
        domain_with_three_consumers["domain_id"],
    )
    topic = next(
        ref
        for ref in detail["used_by"]["refs"]
        if ref["id"] == domain_with_three_consumers["topic_id"]
    )
    # The label is the topic's OWN title, read from its owner's table -- not the
    # id dressed up, and not a name this module invented.
    assert topic["label"].startswith("Topic ")
    assert topic["recorded_at"] is not None
    assert topic["owner_href"]["workspace"] == "context-hub"
    assert topic["owner_href"]["object_type"] == "context-topic"
    assert topic["owner_href"]["object_id"] == domain_with_three_consumers["topic_id"]
    assert topic["relation"] == "explains"


def test_a_target_with_no_screen_keeps_its_workspace_and_gets_no_link(
    live_postgres, domain_with_three_consumers
):
    """`app.target_fields` has no Level-3 route. The reference says so by
    emitting no href, rather than building an address that opens nothing."""
    detail = _compose(
        live_postgres,
        domain_with_three_consumers,
        "business-domain",
        domain_with_three_consumers["domain_id"],
    )
    field = next(
        ref
        for ref in detail["used_by"]["refs"]
        if ref["id"] == domain_with_three_consumers["field_name"]
    )
    assert field["workspace"] == "data"
    assert field["owner_href"] is None


def test_a_withdrawn_link_stops_being_a_consumer(live_postgres, domain_with_three_consumers):
    """Migration 306's rule, held on the list and not only on the count."""
    from core import business_taxonomy

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.mdm_business_links "
            "WHERE taxonomy_id = %s AND target_type = 'topic'",
            (domain_with_three_consumers["domain_id"],),
        )
        link_id = str(cur.fetchone()[0])
    business_taxonomy.retire_link(
        live_postgres,
        org_id=domain_with_three_consumers["org_id"],
        project_id=domain_with_three_consumers["project_id"],
        link_id=link_id,
        actor=ACTOR,
        reason="withdrawn by the operator",
    )
    live_postgres.commit()

    detail = _compose(
        live_postgres,
        domain_with_three_consumers,
        "business-domain",
        domain_with_three_consumers["domain_id"],
    )
    assert detail["used_by"]["count"] == 2
    assert domain_with_three_consumers["topic_id"] not in {
        ref["id"] for ref in detail["used_by"]["refs"]
    }


def test_an_unreadable_consumer_store_is_unavailable_and_never_an_empty_list(
    live_postgres, domain_with_three_consumers
):
    """Fail closed. A partial or absent list must not read as a complete one."""
    from core import governance_read_model

    detail = {
        "summary": {},
        "owner": {"kind": "business-taxonomy"},
        "used_by": {"state": "available", "count": 3, "refs": [], "truncated": False},
        "versions": {"state": "empty", "count": 0, "refs": [], "truncated": False},
    }

    class _Offline:
        def cursor(self):
            raise RuntimeError("consumer store offline")

    governance_read_model._enrich_master_data_object(
        _Offline(),
        domain_with_three_consumers["org_id"],
        "business-domain",
        domain_with_three_consumers["domain_id"],
        detail,
        project_id=domain_with_three_consumers["project_id"],
    )
    assert detail["used_by"]["state"] == "unavailable"
    assert detail["used_by"]["count"] == 3
    assert detail["used_by"]["reason"]["code"] == "master_data_used_by_unreadable"


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@pytest.fixture()
def product_with_three_versions(live_postgres, domain_with_three_consumers):
    """One client-declared Product identity carrying three published revisions."""
    from core import master_data

    registry = master_data.create_registry(
        live_postgres,
        org_id=domain_with_three_consumers["org_id"],
        project_id=domain_with_three_consumers["project_id"],
        object_kind="product",
        label="Products",
        actor=ACTOR,
        version_scope="node",
    )
    node = master_data.create_node(
        live_postgres,
        org_id=domain_with_three_consumers["org_id"],
        project_id=domain_with_three_consumers["project_id"],
        registry_id=str(registry["id"]),
        node_kind="product",
        label="Signature Blend 250g",
        actor=ACTOR,
    )
    for weight in ("250g", "500g", "1kg"):
        version = master_data.create_node_version(
            live_postgres,
            org_id=domain_with_three_consumers["org_id"],
            registry_id=str(registry["id"]),
            node_id=str(node["id"]),
            payload={"weight": weight},
            actor=ACTOR,
        )
        master_data.publish_node_version(
            live_postgres,
            org_id=domain_with_three_consumers["org_id"],
            version_id=str(version["id"]),
            actor=ACTOR,
        )
    live_postgres.commit()
    return {**domain_with_three_consumers, "node_id": str(node["id"])}


def test_three_published_versions_are_three_version_refs(
    live_postgres, product_with_three_versions
):
    """The second half of the verdict: `versions` counted three and listed none."""
    detail = _compose(
        live_postgres,
        product_with_three_versions,
        "master-data-object",
        product_with_three_versions["node_id"],
    )
    facet = detail["versions"]

    assert facet["state"] == "available"
    assert facet["count"] == 3
    assert [ref["version"] for ref in facet["refs"]] == [3, 2, 1]
    # A version states its OWN lifecycle, never its position in the list.
    assert facet["refs"][0]["state"] == "current"
    assert {ref["state"] for ref in facet["refs"][1:]} == {"superseded"}
    assert all(ref["id"].startswith("mdver_") for ref in facet["refs"])
    assert all(ref["owner_href"]["tab"] == "versions" for ref in facet["refs"])


def test_an_exact_product_version_opens_because_the_refs_exist(
    live_postgres, product_with_three_versions
):
    """`compose_governance_object_version` resolves a pin out of `versions.refs`.

    With an empty refs list every historical pin on a Product answered "not
    found", so the Versions tab could count three and open none of them.
    """
    from core.governance_read_model import compose_governance_object_version

    detail = _compose(
        live_postgres,
        product_with_three_versions,
        "master-data-object",
        product_with_three_versions["node_id"],
    )
    superseded = detail["versions"]["refs"][1]["id"]

    envelope = compose_governance_object_version(
        product_with_three_versions["project_id"],
        "master-data",
        "master-data-object",
        product_with_three_versions["node_id"],
        superseded,
        live_postgres,
        org_id=product_with_three_versions["org_id"],
    )
    assert envelope["version_state"] == "stale"
    assert envelope["object"]["selected_version_ref"]["id"] == superseded
