"""LE CLIQUET d'AI-299 : une table portant `org_id` porte une politique, ou une raison.

CE QU'IL EMPECHE. Les 66 tables couvertes avant le 2026-08-17 avaient ete armees
A LA MAIN, une ou deux a la fois, sur 25 migrations -- aucune boucle, aucun
registre. Personne n'a jamais eu la liste sous les yeux, donc chaque table neuve
portant `org_id` sortait du perimetre en silence : 71 s'etaient accumulees. La
migration 271 ferme l'ecart du jour. Ce fichier est ce qui l'empeche de se
reformer a la table suivante.

CE N'EST PAS UN TEST DE DONNEES. Il ne regarde aucune ligne : il lit le schema.
Une table sans politique le fait echouer meme vide, ce qui est le seul moment ou
la reparation coute peu.
"""

from __future__ import annotations

import pytest
from core.db import background_connection

pytestmark = pytest.mark.usefixtures("live_postgres")

RAISON = "cliquet AI-299 : lecture de schema, aucune identite en jeu"

#: HORS PERIMETRE, ET POURQUOI. Armer l'une de ces trois fermerait le chemin qui
#: cree l'appartenance elle-meme -- la politique demanderait d'etre deja membre
#: pour lire ce qui vous rend membre. Ajouter une entree ici est autorise ; le
#: faire sans ecrire la raison ne l'est pas, et la relecture du diff le voit.
HORS_PERIMETRE = {
    "invitations":
        "une invitation se lit AVANT d'etre membre -- c'est sa definition. "
        "L'armer fermerait le seul chemin par lequel un invite devient membre.",
    "instance_claims":
        "revendication d'instance self-hosted : elle precede toute appartenance.",
    "hosted_entry_scope_consumptions":
        "consommation du lien d'entree hebergee : elle CREE l'org et son premier "
        "membre, donc elle s'execute quand personne n'est encore membre.",
}

_TABLES_ORG_SCOPEES = """
    SELECT col.table_name, r.relrowsecurity, r.relforcerowsecurity
    FROM information_schema.columns col
    JOIN pg_class r ON r.relname = col.table_name
    JOIN pg_namespace n ON n.oid = r.relnamespace AND n.nspname = 'app'
    WHERE col.table_schema = 'app' AND col.column_name = 'org_id'
      AND r.relkind = 'r'
    ORDER BY 1
"""


def _tables() -> list[tuple[str, bool, bool]]:
    with background_connection(RAISON) as conn:
        with conn.cursor() as cur:
            cur.execute(_TABLES_ORG_SCOPEES)
            return cur.fetchall()


def test_toute_table_org_scopee_porte_une_politique() -> None:
    nues = sorted(
        t for t, rls, _ in _tables() if not rls and t not in HORS_PERIMETRE
    )
    assert nues == [], (
        f"{len(nues)} table(s) portent `org_id` sans aucune politique RLS : "
        f"{nues}. Le plancher ne peut pas tomber sur ce qu'il ne couvre pas. "
        "Armer la table sur le modele de la migration 271 -- le predicat se "
        "choisit sur la FORME de la table : project_id NOT NULL -> scope "
        "'project' ; project_id NULLABLE -> la meme chose plus une branche "
        "d'appartenance pour les lignes de portee org ; datastream_id -> scope "
        "'flux' ; org_id seul -> app.epic36_is_org_member(org_id). Si la table "
        "doit rester dehors, l'inscrire dans HORS_PERIMETRE AVEC SA RAISON."
    )


def test_une_politique_est_aussi_forcee() -> None:
    """ENABLE sans FORCE ne s'applique pas au proprietaire de la table.

    Une table `ENABLE`d mais non `FORCE`d se lit entierement des que la session
    est son proprietaire -- une isolation qui se croit posee et ne l'est pas.
    """
    tiedes = sorted(t for t, rls, force in _tables() if rls and not force)
    assert tiedes == [], (
        f"{len(tiedes)} table(s) ont ENABLE ROW LEVEL SECURITY sans FORCE : "
        f"{tiedes}. Ajouter `ALTER TABLE app.<t> FORCE ROW LEVEL SECURITY;`."
    )


def test_les_exclusions_existent_encore() -> None:
    """Une exclusion qui survit a la table qu'elle excusait est un trou futur."""
    with background_connection(RAISON) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema='app' AND column_name='org_id' "
                "AND table_name = ANY(%s)",
                (list(HORS_PERIMETRE),),
            )
            vivantes = {r[0] for r in cur.fetchall()}
    fantomes = sorted(set(HORS_PERIMETRE) - vivantes)
    assert fantomes == [], (
        f"HORS_PERIMETRE nomme {fantomes}, qui ne porte(nt) plus `org_id` (ou "
        "n'existe(nt) plus). Retirer l'entree : une exemption sans sujet finira "
        "par excuser une table homonyme qui, elle, en aurait besoin."
    )


def test_la_fonction_de_portee_org_est_security_definer() -> None:
    """Sans SECURITY DEFINER elle exigerait un acces direct a `org_members`."""
    with background_connection(RAISON) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosecdef, provolatile FROM pg_proc "
                "WHERE proname = 'epic36_is_org_member'"
            )
            row = cur.fetchone()
    assert row is not None, "app.epic36_is_org_member absente : migration 271 non appliquee"
    secdef, volatilite = row
    assert secdef, "epic36_is_org_member n'est pas SECURITY DEFINER"
    assert volatilite == "s", (
        "epic36_is_org_member doit etre STABLE : une fonction VOLATILE dans un "
        "predicat de politique est re-evaluee par ligne."
    )
