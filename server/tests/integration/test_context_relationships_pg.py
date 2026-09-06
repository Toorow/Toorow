"""The relationship authority against a real database (story 49-6 AC5).

FIVE PROPERTIES CANNOT BE PROVED ANYWHERE ELSE, and each of them is a sentence
of the story's *Incomplete if*:

  * a version row cannot be UPDATEd or DELETEd -- and the RGPD hatch still opens
    it, which is migration 099/200/209's doctrine applied to the table added
    after them;
  * UPDATE is not merely un-granted on the versions, it is REVOKED (migration
    316's lesson: under 207's default privileges a narrow GRANT is a comment);
  * the CHECK refuses a Master Data <-> Master Data relation even if the code
    one day forgets to -- two locks, because the story calls this one out by
    name;
  * migration 317's backfill is DETERMINISTIC and idempotent, and the
    content_hash it computes in SQL is byte for byte the one
    `context_relationships.relationship_hash` computes in Python;
  * the RLS floor withholds a relation from a subject granted another project.

It runs as the ordinary `connector` role; `live_postgres` rolls back on
teardown.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

psycopg = pytest.importorskip("psycopg")

from core import context_relationships as relationships  # noqa: E402
from core.query_specs_api import arm_access_floor  # noqa: E402
from ulid import ULID  # noqa: E402

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "317_a_context_relation_has_one_writer_and_a_version_it_cannot_rewrite.sql"
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _assert_rls_can_bite(cur) -> None:
    """A superuser or a BYPASSRLS role makes an isolation assertion vacuous."""
    cur.execute(
        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user"
    )
    user, is_super, bypasses = cur.fetchone()
    assert not is_super, f"connected as superuser {user!r}: RLS is bypassed"
    assert not bypasses, f"role {user!r} has BYPASSRLS: this test proves nothing"


@pytest.fixture()
def world(live_postgres):
    """One org, two projects, two topics and one governed field name."""
    org_id = _uid("org")
    project_a, project_b = _uid("proj"), _uid("proj")
    topic_one, topic_two = _uid("top"), _uid("top")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, '49-6 fixture', %s, 'active', 'test')",
            (org_id, org_id.replace("_", "-")),
        )
        for project_id in (project_a, project_b):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, '49-6 fixture', %s, 'test')",
                (project_id, org_id, project_id.replace("_", "-")),
            )
        for topic_id, title in ((topic_one, "Attribution"), (topic_two, "Spend")):
            cur.execute(
                "INSERT INTO app.context_topics "
                "(id, project_id, title, body_md, status, created_by) "
                "VALUES (%s, %s, %s, 'body', 'active', 'test')",
                (topic_id, project_a, f"{title} {topic_id}"),
            )
    return {
        "org_id": org_id,
        "project_a": project_a,
        "project_b": project_b,
        "topic_one": topic_one,
        "topic_two": topic_two,
    }


def _declare(conn, world, **overrides):
    payload = {
        "project_id": world["project_a"],
        "source_type": "topic",
        "source_id": world["topic_one"],
        "target_type": "topic",
        "target_id": world["topic_two"],
        "relationship_kind": "relates_to",
        "actor": "owner@example.com",
    }
    payload.update(overrides)
    return relationships.create_relationship(conn, **payload)


# ---------------------------------------------------------------------------
# The gesture, end to end
# ---------------------------------------------------------------------------


def test_declaring_writes_the_authority_the_version_and_the_projection(
    live_postgres, world
):
    relation = _declare(live_postgres, world)

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT org_id, status, current_version_id, projection_edge_id "
            "FROM app.context_relationships WHERE id = %s",
            (relation["id"],),
        )
        org_id, status, current_version, edge_id = cur.fetchone()
        cur.execute(
            "SELECT version_number, lifecycle, content_hash "
            "FROM app.context_relationship_versions WHERE relationship_id = %s",
            (relation["id"],),
        )
        versions = cur.fetchall()
        cur.execute(
            "SELECT from_id, to_id, edge_type, project_id FROM app.context_graph "
            "WHERE id = %s",
            (edge_id,),
        )
        edge = cur.fetchone()

    # THE ORGANIZATION IS DERIVED, never sent in.
    assert org_id == world["org_id"]
    assert status == "active"
    assert versions == [
        (1, "created", relation["versions"][0]["content_hash"])
    ]
    assert current_version == relation["versions"][0]["id"]
    # The projection carries the SAME fact, in the same transaction.
    assert edge == (world["topic_one"], world["topic_two"], "relates_to", world["project_a"])


def test_retiring_keeps_the_relation_and_removes_only_the_projection(
    live_postgres, world
):
    """*Retirement is a supersede, never a delete* -- proved by what survives."""
    relation = _declare(live_postgres, world)
    edge_id = relation["projection_edge_id"]

    relationships.supersede_relationship(
        live_postgres,
        project_id=world["project_a"],
        relationship_id=relation["id"],
        actor="owner@example.com",
        reason="superseded by a narrower link",
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT status, projection_edge_id FROM app.context_relationships "
            "WHERE id = %s",
            (relation["id"],),
        )
        assert cur.fetchone() == ("superseded", None)
        cur.execute("SELECT count(*) FROM app.context_graph WHERE id = %s", (edge_id,))
        assert cur.fetchone()[0] == 0, "the projection outlived the relation it projects"

    read = relationships.read_relationship(
        live_postgres, project_id=world["project_a"], relationship_id=relation["id"]
    )
    lifecycles = [version["lifecycle"] for version in read["versions"]]
    assert lifecycles == ["created", "superseded"]
    # `superseded_by` is DERIVED from the successor's `supersedes_version_id`;
    # the column cannot exist on an append-only table (migration 317's header).
    assert read["versions"][0]["superseded_by"] == read["versions"][1]["id"]
    assert read["versions"][1]["superseded_by"] is None


def test_a_retired_relation_has_a_way_back_and_the_projection_comes_with_it(
    live_postgres, world
):
    relation = _declare(live_postgres, world)
    relationships.supersede_relationship(
        live_postgres,
        project_id=world["project_a"],
        relationship_id=relation["id"],
        actor="owner@example.com",
    )
    restored = relationships.restore(
        live_postgres,
        project_id=world["project_a"],
        relationship_id=relation["id"],
        actor="owner@example.com",
    )

    assert restored["status"] == "active"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.context_graph WHERE id = %s",
            (restored["projection_edge_id"],),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT lifecycle FROM app.context_relationship_versions "
            "WHERE relationship_id = %s ORDER BY version_number",
            (relation["id"],),
        )
        assert [row[0] for row in cur.fetchall()] == [
            "created",
            "superseded",
            "restored",
        ]


def test_a_relation_retired_then_re_declared_refuses_the_restore(live_postgres, world):
    """The way back is not a way to two live relations for one fact."""
    relation = _declare(live_postgres, world)
    relationships.supersede_relationship(
        live_postgres,
        project_id=world["project_a"],
        relationship_id=relation["id"],
        actor="owner@example.com",
    )
    _declare(live_postgres, world)

    with pytest.raises(relationships.ContextRelationshipRefused) as refusal:
        relationships.restore(
            live_postgres,
            project_id=world["project_a"],
            relationship_id=relation["id"],
            actor="owner@example.com",
        )
    assert refusal.value.code == "relation_already_exists"


# ---------------------------------------------------------------------------
# Append-only, and the hatch
# ---------------------------------------------------------------------------


def test_a_version_row_cannot_be_updated_or_deleted(live_postgres, world):
    relation = _declare(live_postgres, world)
    version_id = relation["versions"][0]["id"]

    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT before_update")
        with pytest.raises(psycopg.errors.Error):
            cur.execute(
                "UPDATE app.context_relationship_versions SET lifecycle = 'restored' "
                "WHERE id = %s",
                (version_id,),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_update")

        cur.execute("SAVEPOINT before_delete")
        with pytest.raises(psycopg.errors.Error):
            cur.execute(
                "DELETE FROM app.context_relationship_versions WHERE id = %s",
                (version_id,),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_delete")


def test_the_immutability_trigger_yields_to_a_flagged_erasure(live_postgres, world):
    """An append-only table that forgets the hatch re-blocks an org erasure.

    This repository has repaired that twice (migrations 200, 209 and the
    conformance guard beside them). The clause is proved here rather than read.
    """
    relation = _declare(live_postgres, world)
    version_id = relation["versions"][0]["id"]

    with live_postgres.cursor() as cur:
        cur.execute("SELECT set_config('app.rgpd_erasure', 'on', true)")
        cur.execute(
            "DELETE FROM app.context_relationship_versions WHERE id = %s",
            (version_id,),
        )
        cur.execute(
            "SELECT count(*) FROM app.context_relationship_versions WHERE id = %s",
            (version_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT set_config('app.rgpd_erasure', 'off', true)")


def test_update_is_revoked_on_the_versions_not_merely_unmentioned(live_postgres):
    """Migration 316's lesson, pinned at the table that learned it.

    207's `ALTER DEFAULT PRIVILEGES` hands `connector` UPDATE on every table
    created after it, so `GRANT SELECT, INSERT` states a posture it does not
    enforce. 317 writes the REVOKE beside the GRANT.
    """
    with live_postgres.cursor() as cur:
        _assert_rls_can_bite(cur)
        cur.execute(
            "SELECT has_table_privilege('connector', "
            "'app.context_relationship_versions', %s)",
            ("UPDATE",),
        )
        assert cur.fetchone()[0] is False
        for privilege in ("SELECT", "INSERT", "DELETE"):
            cur.execute(
                "SELECT has_table_privilege('connector', "
                "'app.context_relationship_versions', %s)",
                (privilege,),
            )
            assert cur.fetchone()[0] is True, privilege


def test_the_schema_refuses_a_master_data_internal_relation(live_postgres, world):
    """Two locks on one refusal, because the story names this one.

    `context_relationships.create_relationship` refuses it with a sentence; the
    CHECK refuses it whatever any door does. A second authority for a Master
    Data link cannot be opened by forgetting a branch.
    """
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT before_mdm")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO app.context_relationships
                    (id, org_id, project_id, source_type, source_id, target_type,
                     target_id, relationship_kind, provenance, created_by)
                VALUES (%s, %s, %s, 'master_data_node', 'mdnode_A',
                        'master_data_node', 'mdnode_B', 'relates_to', 'test', 'test')
                """,
                (
                    f"crel_{ULID()}",
                    world["org_id"],
                    world["project_a"],
                ),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_mdm")


def test_a_relation_carries_a_whole_scope_or_none_of_it(live_postgres, world):
    """An org without a project is a scope no reader can apply."""
    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT before_half_scope")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO app.context_relationships
                    (id, org_id, project_id, source_type, source_id, target_type,
                     target_id, relationship_kind, provenance, created_by)
                VALUES (%s, %s, NULL, 'topic', 'top_A', 'topic', 'top_B',
                        'relates_to', 'test', 'test')
                """,
                (f"crel_{ULID()}", world["org_id"]),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_half_scope")


# ---------------------------------------------------------------------------
# The backfill -- read from the migration, never re-typed here
# ---------------------------------------------------------------------------


def _backfill_statements() -> list[str]:
    """The three statements migration 317 carries its rows with.

    READ FROM THE MIGRATION, never copied into this file. An instrument that
    measures its own copy of the thing under test measures nothing -- and this
    repository has met that defect often enough to have written it down.
    """
    body = MIGRATION.read_text(encoding="utf-8")
    start = body.index("INSERT INTO app.context_relationships\n")
    end = body.index("DO $proof$")
    section = body[start:end]
    section = re.sub(r"^\s*--.*$", "", section, flags=re.M)
    return [statement.strip() for statement in section.split(";") if statement.strip()]


def _seed_incumbent_edge(conn, world, *, from_id, to_id, edge_type="explains"):
    edge_id = _uid("edge")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.context_graph "
            "(id, from_id, from_type, to_id, to_type, edge_type, project_id, created_by) "
            "VALUES (%s, %s, 'topic', %s, 'topic', %s, %s, 'seed')",
            (edge_id, from_id, to_id, edge_type, world["project_a"]),
        )
    return edge_id


def test_the_backfill_carries_every_edge_once_and_a_re_run_changes_nothing(
    live_postgres, world
):
    """Deterministic ids: the second run mints the same ones and inserts none.

    A backfill whose ids come from a generator is not replayable -- an
    interrupted migration would mint a second relation for every edge on its
    next attempt. 317 derives each id from the incumbent edge id by sha256.
    """
    edges = [
        _seed_incumbent_edge(
            live_postgres, world, from_id=world["topic_one"], to_id=world["topic_two"]
        ),
        _seed_incumbent_edge(
            live_postgres,
            world,
            from_id=world["topic_two"],
            to_id=world["topic_one"],
            edge_type="depends_on",
        ),
    ]
    statements = _backfill_statements()

    def carry():
        with live_postgres.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    carry()
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT projection_edge_id, id, current_version_id FROM "
            "app.context_relationships WHERE projection_edge_id = ANY(%s) "
            "ORDER BY projection_edge_id",
            (edges,),
        )
        first = cur.fetchall()
    assert len(first) == 2
    assert all(row[2] is not None for row in first), "a carried relation has no version"

    carry()
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT projection_edge_id, id, current_version_id FROM "
            "app.context_relationships WHERE projection_edge_id = ANY(%s) "
            "ORDER BY projection_edge_id",
            (edges,),
        )
        second = cur.fetchall()
    assert second == first, "a second run of the backfill was not a no-op"


def test_the_backfill_hash_is_the_one_python_writes(live_postgres, world):
    """One document, two languages, and they must agree byte for byte.

    If they did not, a relation carried by the migration and the same relation
    declared through the service would carry different identities -- and nothing
    would ever notice, because neither side reads the other's hash.
    """
    edge_id = _seed_incumbent_edge(
        live_postgres, world, from_id=world["topic_one"], to_id=world["topic_two"]
    )
    with live_postgres.cursor() as cur:
        for statement in _backfill_statements():
            cur.execute(statement)
        cur.execute(
            "SELECT v.content_hash FROM app.context_relationship_versions v "
            "JOIN app.context_relationships r ON r.id = v.relationship_id "
            "WHERE r.projection_edge_id = %s",
            (edge_id,),
        )
        stored = cur.fetchone()[0]

    assert stored == relationships.relationship_hash(
        project_id=world["project_a"],
        source=relationships.Endpoint("topic", world["topic_one"]),
        target=relationships.Endpoint("topic", world["topic_two"]),
        relationship_kind="explains",
    )


def test_the_backfill_carries_the_kind_verbatim_even_outside_the_vocabulary(
    live_postgres, world
):
    """An incumbent edge predates the vocabulary, and is carried, not judged.

    `context_store.py:1569` records that free-form strings made the graph a pile
    of vocabularies before the six kinds were enforced. The SERVICE holds the
    vocabulary at the door that mints new relations; the migration carries what
    is already there. Refusing it would drop an incumbent edge, which the
    story's *Incomplete if* forbids outright.
    """
    edge_id = _seed_incumbent_edge(
        live_postgres,
        world,
        from_id=world["topic_one"],
        to_id=world["topic_two"],
        edge_type="relation_invalide_xyz",
    )
    with live_postgres.cursor() as cur:
        for statement in _backfill_statements():
            cur.execute(statement)
        cur.execute(
            "SELECT relationship_kind, provenance FROM app.context_relationships "
            "WHERE projection_edge_id = %s",
            (edge_id,),
        )
        assert cur.fetchone() == ("relation_invalide_xyz", "backfill:context_graph")

    # And the door still refuses to mint a NEW one with that kind.
    with pytest.raises(relationships.ContextRelationshipRefused) as refusal:
        _declare(live_postgres, world, relationship_kind="relation_invalide_xyz")
    assert refusal.value.code == "unknown_relationship_kind"


# ---------------------------------------------------------------------------
# The facet, and the floor
# ---------------------------------------------------------------------------


def test_the_facet_reads_both_directions_out_of_the_real_store(live_postgres, world):
    _declare(live_postgres, world)
    facet = relationships.list_for_node(
        live_postgres,
        project_id=world["project_a"],
        node_type="topic",
        node_id=world["topic_two"],
    )
    assert facet["state"] == "ready"
    assert facet["outgoing"] == []
    assert [item["other"]["id"] for item in facet["incoming"]] == [world["topic_one"]]


def test_a_relation_is_withheld_from_a_subject_granted_another_project(
    live_postgres, world
):
    """The floor, proved by reading a row rather than by reading a flag.

    With `toorow.enforce_epic36` unset the policy's first disjunct is TRUE and
    every row is visible, so asserting `relrowsecurity` proves nothing. The
    negative control below is what makes the empty result mean isolation.
    """
    relation = _declare(live_postgres, world)
    subject = f"person_{ULID()}"
    with live_postgres.cursor() as cur:
        _assert_rls_can_bite(cur)
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status) "
            "VALUES (%s, %s, %s, 'member', 'active')",
            (_uid("om"), world["org_id"], subject),
        )
        cur.execute(
            "INSERT INTO app.resource_grants "
            "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
            "VALUES (%s, %s, %s, 'project', %s, 'view', 'test')",
            (_uid("rg"), world["org_id"], subject, world["project_b"]),
        )

        cur.execute("SELECT set_config('toorow.enforce_epic36', 'off', true)")
        cur.execute(
            "SELECT count(*) FROM app.context_relationships WHERE id = %s",
            (relation["id"],),
        )
        assert cur.fetchone()[0] == 1, "fixture row absent: the assertion below is vacuous"

    arm_access_floor(live_postgres, subject)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.context_relationships WHERE id = %s",
            (relation["id"],),
        )
        assert cur.fetchone()[0] == 0, (
            "a subject granted only project B read a relation of project A"
        )
        cur.execute(
            "SELECT count(*) FROM app.context_relationship_versions "
            "WHERE relationship_id = %s",
            (relation["id"],),
        )
        assert cur.fetchone()[0] == 0, "the versions leak what the head withholds"
        cur.execute("SELECT set_config('toorow.enforce_epic36', 'off', true)")


def test_the_fact_stores_ids_and_typed_facts_and_nothing_copied(live_postgres, world):
    """No label, no URL, no owner payload -- the story's *Incomplete if*."""
    relation = _declare(live_postgres, world)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT fact FROM app.context_relationship_versions "
            "WHERE relationship_id = %s",
            (relation["id"],),
        )
        fact = cur.fetchone()[0]
    if isinstance(fact, str):
        fact = json.loads(fact)
    assert set(fact) == {"source", "target", "kind", "provenance"}
    assert set(fact["source"]) == {"type", "id"}
    serialized = json.dumps(fact)
    assert "http" not in serialized
    assert "Attribution" not in serialized, "a copied owner label reached the fact"
