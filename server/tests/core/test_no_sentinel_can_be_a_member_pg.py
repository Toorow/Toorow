"""AI-183, la classe : le registre d'appartenance ne contient que des personnes.

CE QUE CE FICHIER EMPECHE DE REVENIR. AI-183 a purge le 2026-08-04 une ligne
`app.org_members` portant `identity='system'`, `role='owner'`, `status='active'`,
puis s'est ferme. Le 2026-08-17 la meme faute etait la, sous le nom `anonymous`,
posee le 2026-08-07 : l'instance avait ete traitee, pas la classe. Un test qui
nommerait `system` seul aurait laisse passer `anonymous` exactement pareil, donc
il les enumere toutes et verifie la contrainte, pas les donnees du jour.

POURQUOI C'EST UNE FAILLE ET PAS UNE COQUETTERIE. `anonymous` est la sentinelle
que `core.db.install_access_context` refuse quand l'authentification est active.
Une ligne active a ce nom rend `app.epic36_has_resource_access()` vrai pour un
acteur que personne n'est -- donc ouvre le plancher RLS a qui presente la
sentinelle. Tant que le role applicatif portait BYPASSRLS la question ne se
posait pas ; depuis la bascule d'AI-100 elle se pose.

La regle vit dans le schema (migration 270) et non dans les six ecrivains de la
table : c'est ce qui la rend vraie aussi pour un `psql` a la main.
"""

from __future__ import annotations

import pytest
from core.db import background_connection
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

#: Toute valeur qu'un humain ne peut pas etre. La chaine blanche-mais-non-vide
#: est la plus vicieuse des quatre : elle passe un test de verite Python.
SENTINELLES = ("anonymous", "system", "", "   ")

RAISON = "test AI-183: la contrainte se lit sur le schema, sans identite"


def _org_existante(cur) -> str:
    cur.execute("SELECT id FROM app.organizations LIMIT 1")
    row = cur.fetchone()
    if row is None:
        org_id = f"org_{ULID()}"
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, 'AI-183 probe', %s, 'active', %s)",
            (org_id, f"ai183-{ULID()}".lower(), f"person_{ULID()}"),
        )
        return org_id
    return row[0]


@pytest.mark.parametrize("sentinelle", SENTINELLES)
def test_une_sentinelle_ne_peut_pas_devenir_membre(sentinelle: str) -> None:
    """L'INSERT est refuse par la base, pas par un appelant qui y aurait pense."""
    import psycopg

    with background_connection(RAISON) as conn:
        with conn.cursor() as cur:
            org_id = _org_existante(cur)
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                    (f"omem_{ULID()}", org_id, sentinelle),
                )
        conn.rollback()


def test_aucune_sentinelle_ne_reste_dans_la_table() -> None:
    """Le controle negatif de la purge : la contrainte est arrivee APRES les
    lignes, donc elle ne dit rien sur celles qui existaient deja."""
    with background_connection(RAISON) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, identity, role, status FROM app.org_members "
                "WHERE identity = ANY(%s) OR btrim(identity) = ''",
                (list(SENTINELLES),),
            )
            restantes = cur.fetchall()
    assert restantes == [], (
        "des sentinelles sont encore membres d'une organisation : "
        f"{restantes}. La migration 270 purge ET contraint ; si la table en "
        "porte encore, c'est que la purge a ete gardee par le garde-fou "
        "« org sans owner humain » -- inscrire un owner humain, puis rejouer."
    )


def test_une_personne_reste_acceptee() -> None:
    """Sans ce test, une contrainte trop large passerait pour une reussite."""
    with background_connection(RAISON) as conn:
        with conn.cursor() as cur:
            org_id = _org_existante(cur)
            cur.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'member', 'active', NOW())",
                (f"omem_{ULID()}", org_id, f"person_{ULID()}"),
            )
        conn.rollback()
