"""La purge d'une organisation, jouée en vrai — clause (c) de `known-debt.json`.

CE QUE CETTE CLAUSE DISAIT, ET ELLE AVAIT RAISON. « Il n'existe toujours aucun
test qui épingle cette purge de bout en bout, donc la réparation n'est gardée
par rien et peut se défaire en silence. » `tests/core/test_org_purge.py` porte
dix tests verts — et ils gardent le **PLANIFICATEUR** : l'ordre des statements,
les règles de rupture de cycle, le refus d'un ledger préservé. Aucun n'ouvre une
base. Le tracker les citait comme « la purge est gardée depuis le 2026-07-25 »,
ce qui confond le plan avec son exécution.

C'EST LE CHEMIN RGPD. Une purge qui se casse en silence laisse des lignes d'une
personne qui a demandé son effacement, et l'endpoint répond quand même
« effacé ». C'est la seule famille de défauts de ce dépôt où la mesure ne peut
pas attendre.

CE QUI EST PROUVÉ ICI, sur une vraie base :

  * le plan s'exécute — l'org perd ses lignes, ET la ligne d'organisation part
    ensuite (`purge_org_tree` ne la touche pas, par contrat) ;
  * l'effacement est BORNÉ : l'organisation voisine ne perd rien. Une purge qui
    déborde est pire qu'une purge qui échoue, parce qu'elle réussit ;
  * un ledger append-only cède — les triggers d'immutabilité refusent un DELETE
    sauf quand l'effacement se signale (migration 098), et c'est exactement le
    verrou qui a rendu la purge impossible pendant des mois ;
  * rien n'est laissé derrière : la transaction est annulée en fin de test, donc
    ce fichier ne peut pas être la cause d'un artefact.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from core.org_purge import MAX_OPERATIONS, plan_purge, purge_org_tree  # noqa: E402

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- la purge ne se prouve pas sans base",
)

ACTOR = "alice@example.com"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


#: Crockford base32, the alphabet the `ck_semantic_*_id` checks spell out.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(prefix: str) -> str:
    body = "".join(_CROCKFORD[b % 32] for b in uuid.uuid4().bytes[:13] * 2)[:26]
    return f"{prefix}{body}"


def _seed_org(conn, label: str) -> dict[str, str]:
    """Une organisation avec un projet et un Datastream -- de quoi avoir un arbre."""
    org_id, project_id, ds_id = _id("org_"), _id("proj_"), _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, label, org_id, ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (project_id, label, project_id, ACTOR, org_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, %s, NULL, 'managed_feed', FALSE, %s, %s)
            """,
            (ds_id, project_id, label, ACTOR, org_id),
        )
    return {"org_id": org_id, "project_id": project_id, "datastream_id": ds_id}


def _seed_semantic_pin(conn, project_id: str) -> dict[str, str]:
    """A published Semantic View version that PINS a published Concept.

    This is the exact shape governance.md called unerasable on 2026-08-18: the
    pin row lives in `app.semantic_view_version_concepts`, whose foreign key to
    `app.semantic_concepts` is `ON DELETE RESTRICT`. Both objects are
    `published` on purpose -- that is what arms the immutability triggers of
    migration 142, so the seed proves the hatch as well as the plan.
    """
    concept_id = _ulid("sc_")
    concept_version_id = _ulid("scv_")
    view_id = _ulid("sv_")
    view_version_id = _ulid("svv_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_concepts
                (id, project_id, kind, name, lifecycle_status, created_by)
            VALUES (%s, %s, 'metric', 'clicks', 'published', %s)
            """,
            (concept_id, project_id, ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name,
                 label, value_type, expression, aggregation, additivity_class,
                 content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'metric', 'clicks', 'Clicks',
                    'integer', '{"op": "column"}'::jsonb, '{"fn": "sum"}'::jsonb,
                    'additive', %s, %s)
            """,
            (concept_version_id, concept_id, project_id, "a" * 64, ACTOR),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (concept_version_id, concept_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_views
                (id, project_id, name, lifecycle_status, created_by)
            VALUES (%s, %s, 'daily_kpi', 'published', %s)
            """,
            (view_id, project_id, ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'daily_kpi', 'Daily KPI', %s, %s, %s)
            """,
            (view_version_id, view_id, project_id, "b" * 64, "c" * 64, ACTOR),
        )
        cur.execute(
            "UPDATE app.semantic_views SET current_version_id = %s WHERE id = %s",
            (view_version_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_version_concepts
                (view_version_id, ordinal, concept_id, concept_version_id, role)
            VALUES (%s, 0, %s, %s, 'metric')
            """,
            (view_version_id, concept_id, concept_version_id),
        )
    return {
        "concept_id": concept_id,
        "concept_version_id": concept_version_id,
        "view_id": view_id,
        "view_version_id": view_version_id,
    }


def _semantic_counts(conn, project_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        out: dict[str, int] = {}
        for table in ("semantic_concepts", "semantic_views"):
            cur.execute(
                f"SELECT COUNT(*) FROM app.{table} WHERE project_id = %s", (project_id,)
            )
            out[table] = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COUNT(*)
              FROM app.semantic_view_version_concepts p
              JOIN app.semantic_view_versions v ON v.id = p.view_version_id
             WHERE v.project_id = %s
            """,
            (project_id,),
        )
        out["semantic_view_version_concepts"] = int(cur.fetchone()[0])
    return out


def _counts(conn, org_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        out = {}
        for table in ("projects", "datastreams"):
            cur.execute(f"SELECT COUNT(*) FROM app.{table} WHERE org_id = %s", (org_id,))
            out[table] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM app.organizations WHERE id = %s", (org_id,))
        out["organizations"] = int(cur.fetchone()[0])
    return out


@requires_postgres
def test_the_purge_erases_the_tree_and_then_the_organization(live_postgres):
    conn = live_postgres
    target = _seed_org(conn, "Cible")

    before = _counts(conn, target["org_id"])
    assert before == {"projects": 1, "datastreams": 1, "organizations": 1}

    report = purge_org_tree(conn, target["org_id"])
    assert report["total_rows"] >= 2

    after_tree = _counts(conn, target["org_id"])
    # `purge_org_tree` NE SUPPRIME PAS la ligne d'organisation, par contrat : le
    # dernier DELETE appartient a l'appelant, qui possede aussi le commit.
    assert after_tree == {"projects": 0, "datastreams": 0, "organizations": 1}

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (target["org_id"],))
        assert cur.rowcount == 1
    assert _counts(conn, target["org_id"])["organizations"] == 0


@requires_postgres
def test_the_purge_stops_at_the_border_of_its_own_organization(live_postgres):
    """Une purge qui deborde est pire qu'une purge qui echoue : elle REUSSIT."""
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    neighbour = _seed_org(conn, "Voisine")

    purge_org_tree(conn, target["org_id"])

    assert _counts(conn, target["org_id"])["projects"] == 0
    # La voisine est intacte, ligne pour ligne.
    assert _counts(conn, neighbour["org_id"]) == {
        "projects": 1,
        "datastreams": 1,
        "organizations": 1,
    }


@requires_postgres
def test_the_plan_that_the_unit_tests_order_is_the_plan_that_runs(live_postgres):
    """Le PLAN et son EXECUTION sont deux choses, et dix tests ne gardaient que la premiere.

    `plan_purge` lit le catalogue et ne touche aucune ligne : c'est ce que les
    tests unitaires prouvent. Ce test-ci prouve l'autre moitie -- que ce meme
    plan, joue contre une vraie base, ne trebuche sur aucune contrainte.
    """
    conn = live_postgres
    target = _seed_org(conn, "Cible")

    plan = plan_purge(conn, target["org_id"])
    assert plan, "le planificateur ne rend aucun statement : il ne mesure plus rien"
    # Chaque rupture de cycle precede tout DELETE -- la propriete que les tests
    # unitaires affirment, verifiee ici sur le plan reellement execute.
    kinds = [op.kind for op in plan]
    if "null" in kinds and "delete" in kinds:
        assert kinds.index("null") < kinds.index("delete")

    report = purge_org_tree(conn, target["org_id"])
    # Le rapport compte ce qui a ete EFFACE, jamais ce qui a ete detache : un
    # detachement remet une colonne a NULL, il ne retire personne.
    assert report["statements"] == len(plan)
    assert report["total_rows"] == sum(report["rows_by_table"].values())


@requires_postgres
def test_an_organization_that_does_not_exist_plans_without_raising(live_postgres):
    """Le defaut qui a rendu la purge impossible pendant des mois se disait ici.

    `plan_purge` levait sur `fk_master_data_nodes_registry_any_scope` AVANT
    toute ecriture -- pour TOUTE organisation, y compris une qui n'existe pas.
    Aucune organisation ne pouvait etre effacee, et c'est le chemin RGPD.
    """
    plan = plan_purge(live_postgres, "org_DOES_NOT_EXIST")
    assert plan, "le plan est vide : la reparation de 2026-08-03 s'est defaite"
    assert {op.kind for op in plan} <= {"null", "delete"}


#: The share of `MAX_OPERATIONS` the plan may occupy before this guard refuses.
#: It fires while the ceiling can still be raised calmly, not on the day a purge
#: is turned away -- the refusal happens on the RGPD path, where a failure means
#: rows of a person who asked to be erased stay behind.
HEADROOM_ALARM = 0.8


@requires_postgres
def test_the_ceiling_still_has_headroom(live_postgres) -> None:
    """The plan size is DERIVED here instead of being written into a comment.

    `org_purge.MAX_OPERATIONS` used to be documented next to a re-measured count
    of the statements the plan emits. That count is a property of the SCHEMA, so
    it grows with every migration that adds an org-scoped table, and each written
    copy of it was stale within weeks -- 2188, then 3082, both wrong by the time
    they were read. A derivable value is not stored: this test recomputes it on
    the live catalog and prints it in its own failure message.

    Vacuity is what would make this pass on nothing, so the width of the plan is
    asserted too: a traversal that collapsed to a handful of tables would sit
    comfortably under any ceiling while erasing almost nothing.

    The assertion below covers the band between the alarm and the ceiling. ABOVE
    the ceiling there is nothing left to assert: `plan_purge` raises
    `plan exceeded N statements` while it is still building, and this test fails
    on that. The band is what buys the warning; the raise is the refusal it is
    meant to arrive before.
    """
    plan = plan_purge(live_postgres, "org_DOES_NOT_EXIST")
    tables = {op.table for op in plan}
    deletes = sum(1 for op in plan if op.kind == "delete")
    detachments = sum(1 for op in plan if op.kind == "null")

    assert len(tables) > 100, (
        "the plan reaches only "
        f"{len(tables)} distinct tables -- the traversal has collapsed, and the "
        "headroom assertion below would pass on an erasure that erases nothing"
    )
    assert deletes + detachments == len(plan)

    alarm = int(MAX_OPERATIONS * HEADROOM_ALARM)
    assert len(plan) < alarm, (
        f"org_purge plans {len(plan)} statements ({len(tables)} distinct tables, "
        f"{deletes} deletes, {detachments} detachments) against a ceiling of "
        f"MAX_OPERATIONS = {MAX_OPERATIONS}. Past {alarm} the margin is too thin "
        "to absorb the next org-scoped table: raise the ceiling in "
        "`core/org_purge.py` deliberately, rather than discovering it when a "
        "purge is refused on the RGPD path."
    )


# ---------------------------------------------------------------------------
# A Semantic View that pins a Concept -- governance.md, clause of 2026-08-18,
# closed by migration 314.
#
# THE DEFECT, MEASURED 2026-08-25 on a disposable Postgres at migration 313:
#
#   FAILING OP: delete app.projects | fk_projects_org
#   ERROR: update or delete on table "semantic_concepts" violates foreign key
#          constraint "semantic_view_version_concepts_concept_id_fkey"
#
# `plan_purge` named `app.semantic_view_version_concepts` in ZERO statements --
# its only visible parent edge was `ON DELETE CASCADE`, which `_FK_GRAPH_SQL`
# filters out -- so the pin rows were left to Postgres, which reached them
# through `semantic_views` on the SAME statement that cascaded into
# `semantic_concepts`. RESTRICT is checked immediately; the sibling cascade had
# not run yet. An organization holding one published View was unerasable.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_view_that_pins_a_concept_no_longer_makes_the_organization_unerasable(
    live_postgres,
):
    """The RGPD path, walked on the exact shape the clause named."""
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    _seed_semantic_pin(conn, target["project_id"])

    before = _semantic_counts(conn, target["project_id"])
    assert before == {
        "semantic_concepts": 1,
        "semantic_views": 1,
        "semantic_view_version_concepts": 1,
    }, "the seed did not build the shape the clause names"

    report = purge_org_tree(conn, target["org_id"])

    assert _semantic_counts(conn, target["project_id"]) == {
        "semantic_concepts": 0,
        "semantic_views": 0,
        "semantic_view_version_concepts": 0,
    }
    # The pin is erased by a NAMED statement, not by a cascade nobody planned:
    # that is what makes the erasure auditable in `rows_by_table`.
    assert report["rows_by_table"].get("app.semantic_view_version_concepts") == 1
    assert report["rows_by_table"].get("app.semantic_concepts") == 1

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (target["org_id"],))
        assert cur.rowcount == 1


@requires_postgres
def test_the_pin_table_is_named_in_the_plan_and_not_left_to_a_cascade(live_postgres):
    """The plan must SEE the pin table -- reverting 314 turns this red.

    Asserted on the plan rather than only on the purge because the purge can
    succeed by accident: on an organization with no published View there is
    nothing for the RESTRICT to refuse. The plan is a property of the schema, so
    it answers even on an id that does not exist.
    """
    plan = plan_purge(live_postgres, "org_DOES_NOT_EXIST")
    tables = {op.table for op in plan}
    assert "app.semantic_view_version_concepts" in tables, (
        "the erasure plan no longer names app.semantic_view_version_concepts: "
        "the `semantic_concepts.project_id` edge is back on ON DELETE CASCADE, "
        "which `_FK_GRAPH_SQL` cannot see (migration 314)"
    )
    assert "app.semantic_concepts" in tables

    order = [op.table for op in plan if op.kind == "delete"]
    assert order.index("app.semantic_view_version_concepts") < order.index(
        "app.semantic_concepts"
    ), "the pin must be deleted BEFORE the Concept it points at"
    assert order.index("app.semantic_concepts") < order.index("app.projects")


@requires_postgres
def test_outside_an_erasure_a_published_semantic_version_is_still_immutable(
    live_postgres,
):
    """314 unblocks the eraser and moves nothing else.

    An erasure is an audited operation that signals itself
    (`SET LOCAL app.rgpd_erasure`). Ordinary traffic does not, and for it the
    rules of migration 142 must read exactly as they did: a published version is
    neither rewritable nor deletable, and a Concept a View pins is not
    detachable by deleting it.
    """
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    seeded = _seed_semantic_pin(conn, target["project_id"])

    refusals = {
        "delete_published_version": (
            "DELETE FROM app.semantic_view_versions WHERE id = %s",
            (seeded["view_version_id"],),
            psycopg.errors.IntegrityConstraintViolation,
        ),
        "rewrite_published_version": (
            "UPDATE app.semantic_view_versions SET label = 'rewritten' WHERE id = %s",
            (seeded["view_version_id"],),
            psycopg.errors.IntegrityConstraintViolation,
        ),
        # The Concept a View pins is not detachable by deleting it either. The
        # refusal that arrives FIRST is the version guard -- deleting the head
        # row cascades into `semantic_concept_versions`, which is published --
        # and the RESTRICT on the pin stands behind it; the catalog assertion
        # below reads that second lock directly rather than guessing which one
        # spoke.
        "delete_pinned_concept": (
            "DELETE FROM app.semantic_concepts WHERE id = %s",
            (seeded["concept_id"],),
            psycopg.errors.IntegrityConstraintViolation,
        ),
    }
    for sql, params, expected in refusals.values():
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT immutability_probe")
            with pytest.raises(expected):
                cur.execute(sql, params)
            # A refused statement aborts the transaction: without the savepoint
            # the NEXT probe would fail on `InFailedSqlTransaction` and this
            # loop would prove one refusal instead of three.
            cur.execute("ROLLBACK TO SAVEPOINT immutability_probe")

    # And the row is still there, untouched, after all three refusals.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label FROM app.semantic_view_versions WHERE id = %s",
            (seeded["view_version_id"],),
        )
        assert cur.fetchone()[0] == "Daily KPI"

    # 314 moved ONE edge and left the pin's own lock alone. Read from the
    # catalog, which is the same place `plan_purge` reads.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname, confdeltype
              FROM pg_constraint
             WHERE contype = 'f'
               AND conname IN ('semantic_view_version_concepts_concept_id_fkey',
                               'semantic_concepts_project_id_fkey')
            """
        )
        deltypes = dict(cur.fetchall())
    assert deltypes["semantic_view_version_concepts_concept_id_fkey"] == "r", (
        "the pin no longer RESTRICTs: a Concept a published View points at "
        "would now vanish under it"
    )
    assert deltypes["semantic_concepts_project_id_fkey"] == "a", (
        "migration 314 is not applied here: `semantic_concepts.project_id` is "
        "back on a delete rule `plan_purge` cannot see"
    )


# ---------------------------------------------------------------------------
# The Knowledge Graph READ PROJECTION -- deferred finding 5 of the adversarial
# review of story 49-6 AC5 (2026-08-28), closed by migration 338.
#
# THE DEFECT. `app.context_graph` was created by migration 031 with NO foreign
# key at all, so it was invisible to BOTH erasure mechanisms: `plan_purge` never
# visited it (the plan is derived from the FK graph) and Postgres had nothing to
# cascade through. Since migration 317 the AUTHORITY -- `app.context_relationships`
# -- hangs off `app.projects` ON DELETE CASCADE, so an org erasure deleted the
# authority rows and STRANDED their projection rows.
#
# What stayed behind was not inert: `context_api.delete_graph_edge` reads a
# projection, finds no relation behind it, and answers 410
# `relation_not_governed`. The erasure manufactured exactly the state that door
# exists to refuse -- on the rows of a person who asked to be forgotten.
# ---------------------------------------------------------------------------


def _seed_graph_relation(conn, project_id: str) -> dict[str, str]:
    """One relation declared through the REAL authority, projection included.

    Written through `context_relationships.create_relationship` rather than by
    INSERT: the projection row is the thing under test, and a hand-written pair
    would prove that two INSERTs can be deleted, not that the relation the
    product mints is reachable by the eraser.
    """
    from core.context_relationships import create_relationship  # noqa: PLC0415

    source_id, target_id = _id("top_"), _id("top_")
    with conn.cursor() as cur:
        for topic_id, title in ((source_id, "Attribution"), (target_id, "Spend")):
            cur.execute(
                """
                INSERT INTO app.context_topics
                    (id, project_id, title, body_md, status, created_by)
                VALUES (%s, %s, %s, '', 'active', %s)
                """,
                (topic_id, project_id, title, ACTOR),
            )

    relation = create_relationship(
        conn,
        project_id=project_id,
        source_type="topic",
        source_id=source_id,
        target_type="topic",
        target_id=target_id,
        relationship_kind="relates_to",
        actor=ACTOR,
    )
    edge_id = relation["projection_edge_id"]
    assert edge_id, "the authority minted no projection: this seed proves nothing"
    return {
        "relationship_id": relation["id"],
        "edge_id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
    }


def _projection_counts(conn, project_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        out: dict[str, int] = {}
        for table in ("context_graph", "context_relationships"):
            cur.execute(
                f"SELECT COUNT(*) FROM app.{table} WHERE project_id = %s", (project_id,)
            )
            out[table] = int(cur.fetchone()[0])
    return out


@requires_postgres
def test_the_knowledge_graph_projection_is_named_in_the_plan(live_postgres):
    """The plan must SEE the projection -- reverting 338 turns this red.

    Asserted on the plan and not only on a purge, because a purge can succeed by
    accident: an organization holding no Knowledge Graph edge leaves nothing
    behind either way. The plan is a property of the SCHEMA, so it answers on an
    id that does not exist and touches no row.
    """
    plan = plan_purge(live_postgres, "org_DOES_NOT_EXIST")
    tables = {op.table for op in plan}
    assert "app.context_graph" in tables, (
        "the erasure plan no longer names app.context_graph: "
        "`context_graph.project_id` has lost the RESTRICT foreign key migration "
        "338 added, and `_FK_GRAPH_SQL` (confdeltype IN ('a','r')) cannot see a "
        "table with no foreign key -- nor a CASCADE one. The projection rows of "
        "an erased organization stay behind, and the DELETE door reports them "
        "as 410 relation_not_governed"
    )

    order = [op.table for op in plan if op.kind == "delete"]
    assert order.index("app.context_graph") < order.index("app.projects"), (
        "the projection must be deleted BEFORE the project it hangs off"
    )


@requires_postgres
def test_a_knowledge_graph_edge_does_not_survive_the_erasure_of_its_organization(
    live_postgres,
):
    """The RGPD path, walked on the exact shape the deferred finding names."""
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    _seed_graph_relation(conn, target["project_id"])

    before = _projection_counts(conn, target["project_id"])
    assert before == {"context_graph": 1, "context_relationships": 1}, (
        "the seed did not build the authority/projection pair the finding names"
    )

    report = purge_org_tree(conn, target["org_id"])

    assert _projection_counts(conn, target["project_id"]) == {
        "context_graph": 0,
        "context_relationships": 0,
    }
    # NAMED by the eraser, not swept up by a cascade nobody planned: that is what
    # makes the erasure auditable in `rows_by_table`, and it is the reason the
    # foreign key is RESTRICT rather than CASCADE.
    assert report["rows_by_table"].get("app.context_graph") == 1, (
        "the projection vanished without `org_purge` naming it: the edge is back "
        f"on a delete rule the plan cannot see. rows_by_table={sorted(report['rows_by_table'])}"
    )

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (target["org_id"],))
        assert cur.rowcount == 1


@requires_postgres
def test_an_orphaned_projection_can_no_longer_be_created_by_deleting_a_project(
    live_postgres,
):
    """The 410 case cannot be born any more -- the schema refuses to strand a row.

    `context_api.delete_graph_edge` answers 410 `relation_not_governed` when a
    projection has no relation behind it, and it is right to: that state means a
    second writer. Before 338 an org erasure MADE that state, because the
    authority cascaded and the projection did not. The proof is not that the
    door behaves differently -- it must not -- but that the state it reports can
    no longer arise.

    HOW IT IS PROVEN, and the shape is deliberate. The project-rooted plan is run
    with the ONE statement about `app.context_graph` REMOVED, and the final
    `DELETE FROM app.projects` is then required to be refused BY NAME. That is
    the pre-338 world reproduced exactly -- an eraser that does not name the
    projection -- and today the database itself refuses to let it end in a
    stranded row. A bare `DELETE FROM app.projects` would not prove this: several
    other RESTRICT children of a project answer first, and which one Postgres
    reports is not ours to decide.

    `plan_purge`'s root is a parameter (AI-291), so this is the production
    planner scoped to one project, not a second traversal written for a test.
    """
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    bare_project = _id("proj_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, 'Cible sans datastream', %s, %s, %s)
            """,
            (bare_project, bare_project, ACTOR, target["org_id"]),
        )
    _seed_graph_relation(conn, bare_project)

    plan = plan_purge(
        conn,
        bare_project,
        root_table="app.projects",
        root_predicate="id = %s",
    )
    assert any(op.table == "app.context_graph" for op in plan), (
        "the project-rooted plan does not name the projection either"
    )

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT strand_probe")
        # The erasure hatch, exactly as `purge_org_tree` opens it: append-only
        # children of a project refuse DELETE without it.
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        for op in plan:
            if op.table == "app.context_graph":
                continue  # the pre-338 eraser: it never named this table
            cur.execute(op.sql, (bare_project,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as excinfo:
            cur.execute("DELETE FROM app.projects WHERE id = %s", (bare_project,))
        cur.execute("ROLLBACK TO SAVEPOINT strand_probe")
    # Read from the error's DIAGNOSTICS, not from its sentence: the message is
    # localised by the server's `lc_messages`.
    assert excinfo.value.diag.constraint_name == "context_graph_project_id_fkey", (
        "the project was deleted (or refused by another child) with its "
        "projection left behind -- the 410 `relation_not_governed` state is "
        f"reachable again. Constraint that answered: {excinfo.value.diag.constraint_name}"
    )

    # And the pair is still whole: a rolled-back probe detached nothing.
    assert _projection_counts(conn, bare_project) == {
        "context_graph": 1,
        "context_relationships": 1,
    }

    # The platform scope stays outside all of this: `project_id IS NULL` is what
    # a platform edge is, and MATCH SIMPLE leaves it unconstrained.
    platform_edge = _id("edge_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_graph
                (id, from_id, from_type, to_id, to_type, edge_type, project_id,
                 created_by)
            VALUES (%s, %s, 'topic', %s, 'topic', 'relates_to', NULL, %s)
            """,
            (platform_edge, _id("top_"), _id("top_"), ACTOR),
        )
        cur.execute(
            "SELECT project_id FROM app.context_graph WHERE id = %s", (platform_edge,)
        )
        assert cur.fetchone()[0] is None


@requires_postgres
def test_the_projection_edge_restricts_rather_than_cascades(live_postgres):
    """Read from the catalog -- the same place `plan_purge` reads.

    CASCADE would also stop the rows from being stranded, and it would make the
    erasure MUTE: `_FK_GRAPH_SQL` filters `confdeltype IN ('a','r')`, so a
    CASCADE-only table is erased by Postgres and named in NONE of the eraser's
    own statements. Migration 337 states this for
    `semantic_recompile_attempts`; `context-hub.md` states it for
    `app.context_events`. This asserts the shape, so a later migration that
    "simplifies" the edge to CASCADE fails here rather than in an audit nobody
    reads.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE contype = 'f' AND conname = 'context_graph_project_id_fkey'"
        )
        row = cur.fetchone()
    assert row is not None, "migration 338 is not applied here"
    assert row[0] == "r", (
        "context_graph_project_id_fkey is no longer ON DELETE RESTRICT: the "
        "projection would be erased by a cascade `org_purge` cannot name"
    )


# ---------------------------------------------------------------------------
# Migration 352 -- the AUTHORITY behind the projection, and its versions.
#
# 338 gave the READ PROJECTION (`app.context_graph`) a RESTRICT parent so the
# eraser NAMES it. The authority 317 created was left on CASCADE while 317's own
# header claimed `core.org_purge` "reaches them through the graph it walks" --
# and `app.context_relationship_versions` never hung off `app.projects` at all.
# Nothing leaked; the mechanism named was false, and the conformance guard let it
# through on an edge that led nowhere (AI-365). These four tests are the pair of
# the four above, on the tables 352 flips.
# ---------------------------------------------------------------------------


def _relation_counts(conn, project_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.context_relationships WHERE project_id = %s",
            (project_id,),
        )
        heads = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM app.context_relationship_versions WHERE project_id = %s",
            (project_id,),
        )
        versions = int(cur.fetchone()[0])
    return {"context_relationships": heads, "context_relationship_versions": versions}


@requires_postgres
def test_the_context_relation_authority_is_named_in_the_plan(live_postgres):
    """The plan must SEE the head AND its versions -- reverting 352 turns this red.

    Asserted on the plan and not only on a purge: an organization holding no
    relation is erased identically either way, so a purge alone cannot tell
    "reached" from "absent". The plan is a property of the SCHEMA, so it answers
    on an id that does not exist and touches no row.
    """
    plan = plan_purge(live_postgres, "org_DOES_NOT_EXIST")
    tables = {op.table for op in plan}
    for table in ("app.context_relationships", "app.context_relationship_versions"):
        assert table in tables, (
            f"the erasure plan no longer names {table}: the foreign key migration "
            "352 set to RESTRICT is back on CASCADE, and `_FK_GRAPH_SQL` "
            "(confdeltype IN ('a','r')) cannot see a CASCADE edge. The rows are "
            "still erased -- by Postgres, from somebody else's statement -- and "
            "the audited erasure report names them nowhere"
        )

    order = [op.table for op in plan if op.kind == "delete"]
    assert order.index("app.context_relationship_versions") < order.index(
        "app.context_relationships"
    ), "the append-only versions must be deleted BEFORE the relation head"
    assert order.index("app.context_relationships") < order.index("app.projects"), (
        "the relation must be deleted BEFORE the project it hangs off"
    )


@requires_postgres
def test_a_context_relation_and_its_versions_do_not_survive_their_organization(
    live_postgres,
):
    """The RGPD path, walked on the authority rather than on its projection."""
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    _seed_graph_relation(conn, target["project_id"])

    before = _relation_counts(conn, target["project_id"])
    assert before == {
        "context_relationships": 1,
        "context_relationship_versions": 1,
    }, f"the seed did not build a relation with a version row: {before}"

    report = purge_org_tree(conn, target["org_id"])

    assert _relation_counts(conn, target["project_id"]) == {
        "context_relationships": 0,
        "context_relationship_versions": 0,
    }
    # NAMED by the eraser, in statements of its own -- that is the whole point of
    # RESTRICT over CASCADE, and it is what makes `rows_by_table` an audit trail
    # rather than a partial one.
    for table in ("app.context_relationships", "app.context_relationship_versions"):
        assert report["rows_by_table"].get(table) == 1, (
            f"{table} vanished without `org_purge` naming it: its rows went on a "
            "delete rule the plan cannot see. rows_by_table="
            f"{sorted(report['rows_by_table'])}"
        )

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (target["org_id"],))
        assert cur.rowcount == 1


@requires_postgres
def test_deleting_a_project_holding_a_relation_is_refused_by_name(live_postgres):
    """A project cannot take its relations down with it in silence any more.

    Same shape as the projection proof above, and for the same reason: the
    project-rooted plan is run with the statements about the two relation tables
    REMOVED -- the pre-352 eraser exactly -- and the final `DELETE FROM
    app.projects` is then required to be refused BY NAME. A bare delete on a
    fully seeded project would not prove this: several other RESTRICT children
    answer first, and which one Postgres reports is not ours to decide.
    """
    conn = live_postgres
    target = _seed_org(conn, "Cible")
    bare_project = _id("proj_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, 'Cible sans datastream', %s, %s, %s)
            """,
            (bare_project, bare_project, ACTOR, target["org_id"]),
        )
    _seed_graph_relation(conn, bare_project)

    plan = plan_purge(
        conn,
        bare_project,
        root_table="app.projects",
        root_predicate="id = %s",
    )
    authority = {"app.context_relationships", "app.context_relationship_versions"}
    assert authority <= {op.table for op in plan}, (
        "the project-rooted plan does not name the relation authority either"
    )

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT relation_probe")
        # The erasure hatch, exactly as `purge_org_tree` opens it: the version
        # ledger is append-only and refuses DELETE without it.
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        for op in plan:
            if op.table in authority:
                continue  # the pre-352 eraser: it named neither table
            cur.execute(op.sql, (bare_project,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as excinfo:
            cur.execute("DELETE FROM app.projects WHERE id = %s", (bare_project,))
        cur.execute("ROLLBACK TO SAVEPOINT relation_probe")
    # Read from the error's DIAGNOSTICS, not from its sentence: the message is
    # localised by the server's `lc_messages`.
    assert excinfo.value.diag.constraint_name == "fk_context_relationships_project", (
        "the project was deleted (or refused by another child) with its context "
        "relations taken along by a cascade nobody planned. Constraint that "
        f"answered: {excinfo.value.diag.constraint_name}"
    )

    # And the pair is still whole: a rolled-back probe detached nothing.
    assert _relation_counts(conn, bare_project) == {
        "context_relationships": 1,
        "context_relationship_versions": 1,
    }


@requires_postgres
def test_the_relation_authority_edges_restrict_rather_than_cascade(live_postgres):
    """Read from the catalog -- the same place `plan_purge` reads.

    Both edges, because flipping only the head would name the head and leave the
    versions swept up by its DELETE, named nowhere. This asserts the shape, so a
    later migration that "simplifies" either edge back to CASCADE fails here
    rather than in an audit nobody reads.
    """
    expected = {
        "fk_context_relationships_project": "r",
        "fk_context_relationship_versions_relation": "r",
    }
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE contype = 'f' AND conname = ANY(%s)",
            (sorted(expected),),
        )
        found = {str(name): str(action) for name, action in cur.fetchall()}
    assert found == expected, (
        "migration 352 is not applied here, or one of its edges is back on a "
        f"delete rule `org_purge` cannot see: {found}"
    )


@requires_postgres
def test_the_eraser_holds_one_column_of_update_and_no_more(live_postgres):
    """The privilege half of the cycle break, pinned in both directions.

    `plan_purge` breaks the version -> predecessor cycle with an UPDATE, and
    PostgreSQL checks the TABLE PRIVILEGE before the trigger: with 352's two
    foreign keys and nothing else, every purge in this file failed with
    `InsufficientPrivilege: permission denied for table
    context_relationship_versions` -- 317 revoked UPDATE there on purpose.

    352 grants back exactly one column. Both halves are asserted, because either
    one alone is the wrong posture: a table-level GRANT would make the whole
    append-only row writable by the application role, and no grant at all makes
    the erasure unrunnable on the RGPD path.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'connector'"
        )
        role = cur.fetchone()
        assert role is not None, "role `connector` is missing: nothing below measures"
        assert not role[0] and not role[1], (
            "`connector` is SUPERUSER or BYPASSRLS here: has_*_privilege answers "
            "TRUE whatever the ACL says, so the assertions below prove nothing"
        )
        cur.execute(
            """
            SELECT
              has_table_privilege('connector',
                  'app.context_relationship_versions', 'UPDATE'),
              has_column_privilege('connector',
                  'app.context_relationship_versions', 'supersedes_version_id', 'UPDATE'),
              has_column_privilege('connector',
                  'app.context_relationship_versions', 'fact', 'UPDATE'),
              has_column_privilege('connector',
                  'app.context_relationship_versions', 'content_hash', 'UPDATE')
            """
        )
        table_update, break_column, fact_column, hash_column = cur.fetchone()

    assert not table_update, (
        "`connector` holds table-level UPDATE on app.context_relationship_versions: "
        "317's REVOKE has been undone, and the append-only ledger is declaratively "
        "rewritable. 352 grants ONE column, not the table."
    )
    assert break_column, (
        "`connector` cannot update `supersedes_version_id`: the cycle-breaking "
        "statement `org_purge` emits for this table is refused with 42501 before "
        "the trigger runs, and the organization erasure cannot complete."
    )
    assert not fact_column and not hash_column, (
        "the grant widened beyond `supersedes_version_id`: the typed fact and its "
        "content hash are what make a version row evidence, and they are not "
        "writable by the application role at any privilege level."
    )
