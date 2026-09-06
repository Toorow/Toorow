"""Le rail machine de la Story 45.8, contre un VRAI Postgres.

POURQUOI CE FICHIER EXISTE, ET IL AURAIT DU EXISTER LE 2026-08-05.

`tests/core/test_agent_files_what_is_missing.py` prouve ce que le producteur
DEPOSE : il remplace `request_review` par une capture et sert les lectures avec
un `MagicMock`. Un `MagicMock` repond a n'importe quelle instruction, donc il
repondait aussi a celle-ci :

    SELECT id, project_id, version_number FROM app.context_topics ...
    -> UndefinedColumn: la colonne << version_number >> n'existe pas

Ni `app.context_topics` ni `app.procedures` n'ont jamais porte cette colonne --
le numero vit dans la table d'historique. Le producteur livre par 45.8 levait
donc a chaque franchissement de seuil, et n'avait jamais depose une seule
remarque. Onze tests verts au-dessus d'un producteur qui ne produisait rien.

CE QUE CE FICHIER TIENT, ET QUE SEUL UN VRAI POSTGRES PEUT TENIR :

  * AC1/AC2 -- une remarque `origin='agent'` EXISTE en base apres le seuil, a la
    version du noeud, et un second franchissement n'en cree pas une seconde ;
  * AC4 -- proposition -> acceptation humaine -> lien `derived`, de bout en bout,
    sur les vraies contraintes. La case etait cochee et
    `grep "resolve_request\\|link_origin=\\"derived\\"" server/tests` ne rendait
    RIEN le 2026-08-05 ;
  * l'etancheite entre locataires du repli d'insertion (migration 215), que
    AUCUN test ne couvrait : tous les cas amont utilisent `org_id="org_1"`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from core import business_taxonomy, candidate_fate, context_review

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres proof skipped",
)


#: ⚠️ DES IDENTIFIANTS FIXES, ET C'EST LA TABLE APPEND-ONLY QUI L'IMPOSE.
#: `trg_procedures_versions_immutable` REFUSE le DELETE
#: (<< Context versions tables are append-only >>), donc une fixture a
#: identifiant tire au hasard laisserait trois lignes de version derriere elle a
#: CHAQUE execution. Le socle est donc pose une fois, en `ON CONFLICT DO
#: NOTHING`, et jamais defait -- exactement le raisonnement que `test_org` porte
#: deja dans `conftest.py`. Seul ce que le test PRODUIT est nettoye.
_PROJECT_ID = "proj_45_8_rail"
_PROCEDURE_ID = "proc_45_8_rail"
_DOMAIN_ID = "bdm_45_8_rail"
_AUTHOR = "owner@example.com"


@contextmanager
def _seeded(conn, org_id: str):
    """Le socle (projet, Skill, trois versions, domaine), puis la file nettoyee.

    Rien n'est moque : les cles etrangeres, les CHECK et l'index unique partiel
    de la migration 215 sont exactement ce que ce fichier vient prouver.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by)"
            " VALUES (%s, %s, '45.8 rail fixture', 'rail-45-8', 'active', %s)"
            " ON CONFLICT (id) DO NOTHING",
            (_PROJECT_ID, org_id, _AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.procedures"
            " (id, project_id, name, description, frontmatter_yaml, body_md,"
            "  status, created_by)"
            " VALUES (%s, %s, 'rail-45-8-skill', 'A skill nobody linked.',"
            "         'name: \"rail-45-8-skill\"\\ndescription: \"d\"\\n', 'body',"
            "         'active', %s)"
            " ON CONFLICT (id) DO NOTHING",
            (_PROCEDURE_ID, _PROJECT_ID, _AUTHOR),
        )
        for version in (1, 2, 3):
            cur.execute(
                "INSERT INTO app.procedures_versions"
                " (procedure_id, project_id, name, description, frontmatter_yaml,"
                "  body_md, status, created_by, created_at, updated_at,"
                "  version_number, changed_by)"
                " VALUES (%s, %s, 'rail-45-8-skill', 'd',"
                "         'name: \"rail-45-8-skill\"\\ndescription: \"d\"\\n',"
                "         'body', 'active', %s, now(), now(), %s, %s)"
                " ON CONFLICT (procedure_id, version_number) DO NOTHING",
                (_PROCEDURE_ID, _PROJECT_ID, _AUTHOR, version, _AUTHOR),
            )
        cur.execute(
            "INSERT INTO app.mdm_business_domains (id, org_id, slug, name, created_by)"
            " VALUES (%s, %s, 'rail-45-8-domain', '45.8 rail domain', %s)"
            " ON CONFLICT (id) DO NOTHING",
            (_DOMAIN_ID, org_id, _AUTHOR),
        )
    conn.commit()
    _clean(conn)
    try:
        yield _PROJECT_ID, _PROCEDURE_ID, _DOMAIN_ID
    finally:
        _clean(conn)


def _clean(conn) -> None:
    """Ce que le test PRODUIT, et rien du socle. Avant ET apres : un test qui ne
    nettoie qu'a la sortie herite du precedent quand celui-ci a echoue."""
    with conn.cursor() as cur:
        # UN LIEN SE RETIRE, IL NE SE DETRUIT PAS -- y compris ici. Le
        # `DELETE` que cette ligne portait etait refuse par
        # `trg_mdm_business_links_no_hard_delete` (migration 306), donc ce
        # nettoyage echouait AVANT le premier test et les trois qui suivent
        # etaient rouges pour une raison qui n'etait pas la leur. La contrainte
        # d'unicite est PARTIELLE depuis la meme migration : un lien retire
        # laisse la place a un lien identique, donc le retrait isole le test
        # aussi bien que la destruction le faisait.
        cur.execute(
            "UPDATE app.mdm_business_links "
            "   SET retired_at = now(), retired_by = %s, retired_reason = %s "
            " WHERE project_id = %s AND retired_at IS NULL",
            (_AUTHOR, "test fixture cleanup", _PROJECT_ID),
        )
        cur.execute(
            "DELETE FROM app.context_review_requests WHERE project_id = %s",
            (_PROJECT_ID,),
        )
        cur.execute(
            "DELETE FROM app.context_candidate_fates WHERE project_id = %s",
            (_PROJECT_ID,),
        )
    conn.commit()


def _crossed(procedure_id: str, times: int = 3) -> list[dict]:
    return [{
        "candidate_id": procedure_id,
        "candidate_kind": "procedure",
        "reason": candidate_fate.REASON_BELOW_CUTOFF,
        "times": times,
        "last_query": "attribution window",
    }]


@requires_postgres
def test_the_threshold_files_a_real_remark_at_the_nodes_real_version(
    live_postgres, test_org
):
    """AC1/AC2 contre la base. Avant le 2026-08-10 ce chemin levait
    `UndefinedColumn` sur les DEUX genres de noeud : zero remarque deposee depuis
    que le producteur existe, et le `MagicMock` des tests unitaires ne pouvait
    pas le voir."""
    conn = live_postgres
    with _seeded(conn, test_org) as (project_id, procedure_id, _domain):
        filed = context_review.flag_recurring_rejections(
            conn, project_id=project_id, crossed=_crossed(procedure_id)
        )
        conn.commit()

        assert len(filed) == 1
        open_rows = context_review.list_open(
            conn, org_id=test_org, project_id=project_id, node_id=procedure_id
        )
        assert len(open_rows) == 1
        row = open_rows[0]
        assert row["origin"] == "agent"
        assert row["requested_by"] == context_review.AGENT_AUTHOR
        # La version est celle du noeud, lue dans la table d'HISTORIQUE.
        assert row["node_version"] == 3
        assert row["proposed_change"] is None


@requires_postgres
def test_the_same_gap_seen_again_is_still_one_open_remark(live_postgres, test_org):
    """AC2 : l'idempotence est CONSTRUITE (la note est derivee du fait), et
    l'index unique partiel de la migration 215 la tient. Trois passages, une
    ligne."""
    conn = live_postgres
    with _seeded(conn, test_org) as (project_id, procedure_id, _domain):
        for times in (3, 4, 91):
            context_review.flag_recurring_rejections(
                conn, project_id=project_id, crossed=_crossed(procedure_id, times)
            )
        conn.commit()

        rows = context_review.list_open(
            conn, org_id=test_org, project_id=project_id, node_id=procedure_id
        )
        assert len(rows) == 1


@requires_postgres
def test_a_machine_proposal_accepted_by_a_human_becomes_a_derived_link(
    live_postgres, test_org
):
    """AC4, DE BOUT EN BOUT -- et la case etait cochee sans qu'aucun test
    n'existe :

        grep -rn "resolve_request\\|link_origin=\\"derived\\"" server/tests
          -> aucune sortie (2026-08-05)

    Ce qui compte n'est pas que le lien existe : c'est qu'il soit distinguable
    POUR TOUJOURS d'une decision humaine.
    """
    conn = live_postgres
    with _seeded(conn, test_org) as (project_id, procedure_id, domain_id):
        proposal = context_review.propose_missing_link(
            conn,
            org_id=test_org,
            project_id=project_id,
            node_type="procedure",
            node_id=procedure_id,
            node_version=3,
            taxonomy_type="business_domain",
            taxonomy_id=domain_id,
            relation_type="applies_to",
            evidence="missing_path on route rte_EXAMPLE (evaluation run evr_EXAMPLE)",
        )
        conn.commit()
        assert proposal["origin"] == "agent"

        resolved = context_review.resolve_request(
            conn,
            request_id=proposal["id"],
            org_id=test_org,
            status="accepted",
            resolved_by="owner@example.com",
        )
        conn.commit()

        assert resolved["status"] == "accepted"
        # Accepter et appliquer ne sont pas le meme fait : `applied_ref` dit ce
        # que l'acceptation a produit.
        assert resolved["applied_ref"]

        links = business_taxonomy.list_links(
            conn, org_id=test_org, project_id=project_id
        )
        derived = [link for link in links if link["id"] == resolved["applied_ref"]]
        assert len(derived) == 1
        assert derived[0]["link_origin"] == "derived"
        assert derived[0]["target_id"] == procedure_id

        # Et la remarque n'est plus dans la file : elle a ete tranchee.
        assert context_review.list_open(
            conn, org_id=test_org, project_id=project_id, node_id=procedure_id
        ) == []


@requires_postgres
def test_a_failure_inside_the_producer_neither_poisons_nor_half_files(
    live_postgres, test_org, monkeypatch
):
    """LE POINT DE REPRISE COUVRE TOUT LE PRODUCTEUR, ET IL N'EN COUVRAIT QU'UNE
    INSTRUCTION -- alors que le commentaire jurait deja l'inverse.

    En psycopg 3 une instruction refusee AVORTE la transaction entiere. Le
    `with conn.transaction()` du 2026-08-05 ne portait que le `SELECT org_id` :
    les lectures de version et les depots tournaient DEHORS, donc l'echec
    empoisonnait la transaction de la RECHERCHE et le `commit` de
    `core/main.py` devenait un rollback silencieux qui emportait les sorts que
    `record_fates` venait d'ecrire.

    La panne est INJECTEE ici plutot que simulee : c'est une vraie instruction
    refusee par Postgres, dans la boucle de depot.
    """
    conn = live_postgres
    with _seeded(conn, test_org) as (project_id, procedure_id, _domain):
        # CE QUE LA RECHERCHE VIENT D'ECRIRE, dans la MEME transaction non
        # commitee -- c'est ce que `record_fates` fait juste avant d'appeler ce
        # producteur, et c'est ce que la portee du point de reprise protege.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.context_candidate_fates"
                " (id, project_id, candidate_id, candidate_kind, reason, last_query)"
                " VALUES ('ccf_45_8_guard', %s, %s, 'procedure', %s, 'q')",
                (project_id, procedure_id, candidate_fate.REASON_BELOW_CUTOFF),
            )

        calls: list[str] = []
        genuine = context_review.flag_recurring_rejection

        def _second_one_explodes(conn_, **kwargs):
            calls.append(kwargs["node_id"])
            if len(calls) == 1:
                return genuine(conn_, **kwargs)
            with conn_.cursor() as cur:
                cur.execute("SELECT no_such_column FROM app.context_review_requests")
            raise AssertionError("unreachable")

        monkeypatch.setattr(
            context_review, "flag_recurring_rejection", _second_one_explodes
        )
        # Deux franchissements DISTINCTS sur le meme noeud : le premier depose,
        # le second casse. Le lot est donc partiellement ecrit quand la panne
        # survient -- le seul etat qui distingue les deux portees.
        crossed = _crossed(procedure_id) + _crossed(procedure_id)
        crossed[1]["reason"] = candidate_fate.REASON_OUT_OF_SCOPE

        filed = context_review.flag_recurring_rejections(
            conn, project_id=project_id, crossed=crossed
        )
        conn.commit()

        # 1. LE TRAVAIL DE L'APPELANT A SURVECU. C'est la propriete que la
        #    portee du point de reprise tient, et la seule qui la distingue :
        #    hors portee, l'echec avorte la transaction entiere et le `commit`
        #    de la recherche devient un rollback silencieux.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.context_candidate_fates WHERE id = %s",
                ("ccf_45_8_guard",),
            )
            assert cur.fetchone()[0] == 1
        # 2. Et RIEN de partiel n'a ete depose : le point de reprise a defait le
        #    producteur, qui ne revendique donc aucun identifiant.
        assert filed == []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.context_review_requests WHERE node_id = %s",
                (procedure_id,),
            )
            assert cur.fetchone()[0] == 0
        conn.commit()


@requires_postgres
def test_a_declined_proposal_closes_without_writing_a_link(live_postgres, test_org):
    """Le controle negatif de l'AC4 : decliner clot la remarque et n'ecrit
    rien. Une file qui applique ce qu'on lui refuse est une file dangereuse."""
    conn = live_postgres
    with _seeded(conn, test_org) as (project_id, procedure_id, domain_id):
        proposal = context_review.propose_missing_link(
            conn,
            org_id=test_org,
            project_id=project_id,
            node_type="procedure",
            node_id=procedure_id,
            node_version=3,
            taxonomy_type="business_domain",
            taxonomy_id=domain_id,
            relation_type="applies_to",
            evidence="missing_path on route rte_EXAMPLE",
        )
        resolved = context_review.resolve_request(
            conn,
            request_id=proposal["id"],
            org_id=test_org,
            status="declined",
            resolved_by="owner@example.com",
        )
        conn.commit()

        assert resolved["status"] == "declined"
        assert resolved["applied_ref"] is None
        assert business_taxonomy.list_links(
            conn, org_id=test_org, project_id=project_id
        ) == []


# ---------------------------------------------------------------------------
# L'etanceite entre locataires du repli d'insertion -- C-1, et son garde
# ---------------------------------------------------------------------------
#
# La relecture du 2026-08-05 nommait DEUX choses : la fuite (fermee depuis, par
# la migration 215 et le predicat `org_id`) et le fait qu'AUCUN test ne prouvait
# l'etancheite de cette table -- tous les cas amont utilisent `org_id="org_1"` et
# font varier `project_id` et la requete, c'est-a-dire les deux champs qui ne
# PEUVENT PAS casser la deduplication. Un garde que rien ne fait tomber peut etre
# retire sans qu'une ligne rougisse.

_SECOND_ORG_ID = "org_45_8_rail_b"


@requires_postgres
def test_two_organizations_filing_the_same_remark_never_read_each_others(
    live_postgres, test_org
):
    """La cle de `uq_context_review_request_open` se repete PAR CONSTRUCTION sur
    un noeud a portee plateforme : `project_id IS NULL`, et `requested_by` vaut
    la constante globale `AGENT_AUTHOR`. Sans `org_id` au repli, le second
    locataire recevait la ligne du premier dans son 201 -- et sa propre remarque
    n'etait jamais deposee."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by)"
            " VALUES (%s, '45.8 rail tenant B', 'rail-45-8-b', 'active', %s)"
            " ON CONFLICT (id) DO NOTHING",
            (_SECOND_ORG_ID, _AUTHOR),
        )
        cur.execute(
            "DELETE FROM app.context_review_requests WHERE node_id = 'proc_PLATFORM_45_8'"
        )
    conn.commit()

    def _file(org_id):
        return context_review.flag_recurring_rejection(
            conn,
            org_id=org_id,
            # Un noeud de PLATEFORME : c'est le cas ou la cle se repete.
            project_id=None,
            node_type="procedure",
            node_id="proc_PLATFORM_45_8",
            node_version=1,
            reason=candidate_fate.REASON_BELOW_CUTOFF,
            times=3,
            last_query="attribution window",
        )

    try:
        first = _file(test_org)
        second = _file(_SECOND_ORG_ID)
        conn.commit()

        assert first["id"] != second["id"]
        assert first["org_id"] == test_org
        assert second["org_id"] == _SECOND_ORG_ID
        # Et le meme fait, revu, rend TOUJOURS la ligne du bon locataire.
        assert _file(_SECOND_ORG_ID)["id"] == second["id"]
        assert _file(test_org)["id"] == first["id"]
        conn.commit()
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.context_review_requests"
                " WHERE node_id = 'proc_PLATFORM_45_8'"
            )
        conn.commit()


@requires_postgres
def test_the_fallback_predicate_is_exactly_the_unique_index_key(live_postgres):
    """Le repli doit refleter la cle, COALESCE compris : plus large il rend une
    ligne etrangere, plus etroit il ne trouve pas celle que l'INSERT vient de
    refuser et on leve sur un doublon legitime. La cle est lue DANS LE CATALOGUE,
    pas recopiee d'un commentaire."""
    import inspect

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = 'app' AND indexname = 'uq_context_review_request_open'"
        )
        row = cur.fetchone()
    assert row is not None, "migration 215 n'a pas ete appliquee"
    indexdef = row[0]

    source = inspect.getsource(context_review.request_review)
    fallback = source[source.index("SELECT {\", \".join(_COLUMNS)}"):]
    for column in ("org_id", "node_type", "node_id", "node_version", "requested_by"):
        assert column in indexdef
        assert column in fallback
    assert "COALESCE(project_id, ''" in indexdef
    assert "COALESCE(project_id, '')" in fallback
    assert "md5(note)" in indexdef and "md5(note)" in fallback
    assert "status = 'open'" in fallback
