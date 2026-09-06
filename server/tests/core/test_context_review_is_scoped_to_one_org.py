"""Une remarque appartient a une organisation -- reparation de la story 45.8.

POURQUOI CE FICHIER EXISTE. La relecture a contexte neuf du 2026-08-05 a
reproduit, contre une base jetable, une fuite entre locataires :

    A filed -> crr_01KZ922RVVT57ZX5F66W47CS1R  org_TENANT_A
    B filed -> crr_01KZ922RVVT57ZX5F66W47CS1R  org_TENANT_A
    same row: True | B queue: [] | rows in table: 1

`uq_context_review_request_open` (migration 202) etait clefe sur
(node_type, node_id, node_version, requested_by, md5(note)) -- pas une de ces
cinq colonnes ne nomme le locataire -- et le repli de `request_review`
re-SELECTait sans predicat `org_id`. Sur un noeud du Hub a portee plateforme,
ou `requested_by` vaut la constante GLOBALE AGENT_AUTHOR, la cle se repete PAR
CONSTRUCTION des que deux organisations rencontrent le meme trou de contexte.

Les deux moities de la reparation sont ici, et chacune a son test :

  * la migration 215 remet `org_id` et `COALESCE(project_id, '')` dans la cle,
    donc l'INSERT de B ne rentre plus en collision avec la ligne de A ;
  * `context_review.request_review` scope son repli, donc meme si une collision
    avait lieu il ne pourrait plus rendre une ligne etrangere.

POURQUOI CE FICHIER EST GATE SUR POSTGRES ET NE PEUT PAS L'ETRE AUTREMENT. Le
defaut EST le comportement d'un index unique partiel. Un curseur moque qui
« echoue » a l'INSERT prouve le repli mais jamais la cle, et c'est la cle qui
faisait la collision. Les tests unitaires existants de 45.8 monkeypatchent
`request_review` et n'ont donc jamais touche cet index -- c'est exactement
pourquoi le defaut a vecu.

TOUT EST ROLLBACK. Aucune ligne ne survit a un test.
"""

from __future__ import annotations

import os

import pytest
from core import context_review

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres proof skipped",
)

#: Le noeud est le MEME pour les deux organisations, et c'est le point : un
#: sujet du Hub a portee plateforme est visible de tout le monde.
_NODE = {"node_type": "topic", "node_id": "top_platform_grain", "node_version": 1}

#: Mot pour mot la meme note, parce que le producteur la DERIVE du fait : le
#: meme trou de contexte rend la meme phrase, chez A comme chez B.
_NOTE = "no governed path reaches the metric 'cost' from this topic"


def _seed_two_orgs(conn) -> tuple[str, str]:
    """Deux organisations, sans projet -- la portee plateforme est le cas nu."""
    with conn.cursor() as cur:
        for org, slug in (("org_TENANT_A", "tenant-a"), ("org_TENANT_B", "tenant-b")):
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (org, org, slug, "owner@example.com"),
            )
    return "org_TENANT_A", "org_TENANT_B"


@requires_postgres
def test_the_same_remark_in_two_organizations_is_two_rows(live_postgres) -> None:
    """La cle porte le locataire : B depose la sienne au lieu d'etre absorbee.

    Sans la migration 215 ce test rougit sur `b["id"] != a["id"]` -- l'INSERT de
    B ne mordait pas et le repli rendait la ligne de A.
    """
    conn = live_postgres
    org_a, org_b = _seed_two_orgs(conn)
    try:
        a = context_review.request_review(
            conn, org_id=org_a, project_id=None, note=_NOTE,
            requested_by=context_review.AGENT_AUTHOR, origin="agent", **_NODE,
        )
        b = context_review.request_review(
            conn, org_id=org_b, project_id=None, note=_NOTE,
            requested_by=context_review.AGENT_AUTHOR, origin="agent", **_NODE,
        )

        assert b["id"] != a["id"], "B a recu la ligne de A -- la cle ignore l'org"
        assert a["org_id"] == org_a
        assert b["org_id"] == org_b, "la ligne rendue a B n'appartient pas a B"
    finally:
        conn.rollback()


@requires_postgres
def test_each_organization_sees_only_its_own_queue(live_postgres) -> None:
    """La file de B n'est pas vide, et ne contient pas la remarque de A.

    C'est la moitie qui COUTAIT le plus : la remarque de B n'etait jamais
    deposee, et definitivement -- le producteur se declenche sur une EGALITE
    (`== RECURRENCE_THRESHOLD`), donc aucun passage ulterieur ne la rattrape.
    """
    conn = live_postgres
    org_a, org_b = _seed_two_orgs(conn)
    try:
        context_review.request_review(
            conn, org_id=org_a, project_id=None, note=_NOTE,
            requested_by=context_review.AGENT_AUTHOR, origin="agent", **_NODE,
        )
        context_review.request_review(
            conn, org_id=org_b, project_id=None, note=_NOTE,
            requested_by=context_review.AGENT_AUTHOR, origin="agent", **_NODE,
        )

        queue_a = context_review.list_open(conn, org_id=org_a, project_id=None)
        queue_b = context_review.list_open(conn, org_id=org_b, project_id=None)

        assert len(queue_a) == 1
        assert len(queue_b) == 1, "la remarque de B a ete absorbee par celle de A"
        assert queue_a[0]["id"] != queue_b[0]["id"]
        assert {r["org_id"] for r in queue_a} == {org_a}
        assert {r["org_id"] for r in queue_b} == {org_b}
    finally:
        conn.rollback()


@requires_postgres
def test_the_same_remark_twice_in_one_organization_is_still_one_row(live_postgres) -> None:
    """La deduplication n'a pas ete perdue en gagnant le cloisonnement.

    C'est le controle negatif de la migration 215. `project_id` est nullable, et
    dans un index unique deux NULL ne sont pas egaux : ajouter la colonne nue
    aurait rendu la deduplication INOPERANTE sur toute remarque a portee
    plateforme -- le defaut d'origine, retourne a l'envers. Le COALESCE est ce
    qui l'empeche, et ce test est ce qui le prouve.
    """
    conn = live_postgres
    org_a, _ = _seed_two_orgs(conn)
    try:
        first = context_review.request_review(
            conn, org_id=org_a, project_id=None, note=_NOTE,
            requested_by=context_review.AGENT_AUTHOR, origin="agent", **_NODE,
        )
        again = context_review.request_review(
            conn, org_id=org_a, project_id=None, note=_NOTE,
            requested_by=context_review.AGENT_AUTHOR, origin="agent", **_NODE,
        )

        assert again["id"] == first["id"], "un doublon a produit une seconde ligne"
        assert len(context_review.list_open(conn, org_id=org_a, project_id=None)) == 1
    finally:
        conn.rollback()
