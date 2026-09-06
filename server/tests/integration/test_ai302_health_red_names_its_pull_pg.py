"""AI-302 -- le rouge de sante nomme le tirage qui l'a pose, et la base le dit.

POURQUOI CETTE MARCHE EXISTE, et pourquoi les tests hors base ne suffisaient pas
ici. Ce que la migration 276 ajoute est un SCHEMA, et ce que les quatre ecrivains
de `app.connection_health` doivent tenir est un INVARIANT ENTRE COLONNES : les
trois `populate_failed_*` ne survivent jamais au statut qu'elles decrivent. Un
curseur simule ne peut pas prouver un `CASE` de `ON CONFLICT DO UPDATE` -- il
enregistre le SQL et le declare correct. Le defaut voisin AI-206 est exactement
ce piege : une chaine prouvee entierement hors base portait six verrous, chacun
invisible tant que le precedent tenait.

CE QUI EST EPROUVE ICI, sur un vrai Postgres, role `connector` :

  * la colonne existe, et le rouge ecrit par `verification` la remplit ;
  * un SECOND rouge remplace le premier -- un `pull_id` perime enverrait
    l'operateur vers une collecte qui n'est plus celle qui ferme la porte ;
  * lever le rouge efface l'etiquette dans la MEME instruction que le statut ;
  * le poller, qui doit garder un `populate_failed` collant, garde AUSSI son
    etiquette -- et la lache avec lui quand `revoked` l'emporte.

Lancer avec une base jetable :

    python scripts/disposable_postgres.py up --port 55503
    eval "$(python scripts/disposable_postgres.py env --port 55503)"
    cd server && python -m pytest tests/integration/test_ai302_health_red_names_its_pull_pg.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

ORG = "org_test_fixture"
PROJECT = "default"
CRED = "cref_ai302_health"
PULL_ONE = "pull_ai302_one"
PULL_TWO = "pull_ai302_two"


@pytest.fixture
def health_row(live_postgres, monkeypatch):
    """Une autorisation reelle et son tirage, puis un nettoyage EXPLICITE.

    `core.db.get_connection` est detourne vers CETTE connexion pour que le code de
    production ecrive dans la meme transaction que les lectures du test -- sinon il
    ouvrirait une session a lui et le test lirait autre chose que ce qui a ete
    ecrit.

    ET LE ROLLBACK DE `live_postgres` NE SUFFIT PAS ICI, mesure et non suppose :
    `_set_connection_health_red` et `clear_connection_health_red` appellent
    `conn.commit()` -- c'est leur contrat, un rouge de sante doit survivre a la fin
    du pull. Detourner la connexion detourne donc AUSSI le commit, et les trois
    lignes de fixture restaient en base : le quatrieme test de ce fichier echouait
    sur `connection_health_pkey` a cause du premier. La sortie efface donc ce que ce
    fichier a ecrit, par identifiant explicite, dans l'ordre des cles etrangeres.
    """
    from contextlib import contextmanager

    from core import db

    conn = live_postgres
    with conn.cursor() as cur:
        # `org_test_fixture` et le projet `default` sont du SOCLE : la fixture de
        # session `test_org` les garantit et ne les detruit jamais, parce que
        # plusieurs fichiers -- voire plusieurs sessions -- s'y rattachent.
        cur.execute(
            """
            INSERT INTO app.connection_ref
                (id, provider, nango_connection_id, project_id, owner_org_id,
                 owner_identity, auth_path)
            VALUES (%s, 'youtube-analytics', NULL, %s, %s, 'owner@example.com',
                    'google_direct')
            ON CONFLICT (id) DO NOTHING
            """,
            (CRED, PROJECT, ORG),
        )
        for pull in (PULL_ONE, PULL_TWO):
            cur.execute(
                """
                INSERT INTO app.pull_jobs
                    (id, pull_id, connection_ref_id, date_from, date_to, state,
                     requested_by)
                VALUES (%s, %s, %s, '2026-07-18', '2026-08-16', 'done',
                        'owner@example.com')
                ON CONFLICT (pull_id) DO NOTHING
                """,
                (f"job_{pull}", pull, CRED),
            )

    @contextmanager
    def _get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", _get_connection)
    try:
        yield conn
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.connection_health WHERE connection_ref_id = %s", (CRED,)
            )
            cur.execute("DELETE FROM app.pull_jobs WHERE connection_ref_id = %s", (CRED,))
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (CRED,))
        conn.commit()


def _read(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, populate_failed_pull_id, populate_failed_verdict,
                   populate_failed_at
            FROM app.connection_health WHERE connection_ref_id = %s
            """,
            (CRED,),
        )
        return cur.fetchone()


def test_the_red_records_the_pull_the_verdict_and_the_date(health_row):
    """LE CONSTAT DU 2026-08-17 : la ligne ne disait que `populate_failed`.

    Ni tirage, ni verdict, ni date propre -- `last_checked_at` est reecrit par
    chaque cycle du poller, donc il dit quand la sante a ete REGARDEE, pas quand le
    rouge a ete POSE. Remonter de « cette autorisation est rouge » a la collecte qui
    l'a rendue rouge demandait une correlation a la main contre le journal des
    tirages, deux fois, a cinq jours d'ecart.
    """
    from core.verification import _set_connection_health_red

    before = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    _set_connection_health_red(CRED, "empty", PULL_ONE)

    status, pull_id, verdict, raised_at = _read(health_row)
    assert status == "populate_failed"
    assert pull_id == PULL_ONE, f"le rouge ne nomme pas son tirage : {pull_id!r}"
    assert verdict == "empty", f"le rouge ne dit pas ce qui a ete mesure : {verdict!r}"
    assert raised_at is not None and raised_at >= before, (
        f"le rouge n'est pas date : {raised_at!r}"
    )


def test_the_most_recent_red_replaces_the_previous_label(health_row):
    """Un `pull_id` perime est une fausse piste, pas une trace.

    Deux fenetres vides d'affilee : la seconde est celle qui ferme la porte
    maintenant, donc c'est elle que la ligne nomme.
    """
    from core.verification import _set_connection_health_red

    _set_connection_health_red(CRED, "empty", PULL_ONE)
    _set_connection_health_red(CRED, "partial", PULL_TWO)

    status, pull_id, verdict, _ = _read(health_row)
    assert (status, pull_id, verdict) == ("populate_failed", PULL_TWO, "partial")


def test_lifting_the_red_drops_its_label_in_the_same_statement(health_row):
    """L'ETIQUETTE APPARTIENT AU ROUGE.

    Laissee sur une ligne qui n'est plus rouge, elle enverrait le lecteur suivant
    vers une collecte qui n'est pas le probleme. Le meme `CASE` porte le statut et
    les trois colonnes, donc ils ne peuvent pas se contredire.
    """
    from core.verification import _set_connection_health_red, clear_connection_health_red

    _set_connection_health_red(CRED, "empty", PULL_ONE)
    clear_connection_health_red(CRED, evidence=f"verified_pull:{PULL_TWO}")

    status, pull_id, verdict, raised_at = _read(health_row)
    assert status == "ok"
    assert (pull_id, verdict, raised_at) == (None, None, None), (
        "une etiquette de rouge survit a la levee du rouge"
    )


def test_a_revoked_status_does_not_keep_lifting_a_red_that_is_not_there(health_row):
    """ET LA LEVEE NE RESSUSCITE RIEN. `revoked` n'est pas repare par une lecture.

    `clear_connection_health_red` ne touche QUE `populate_failed` : une autorisation
    morte reste morte, et son etiquette -- qu'elle n'a pas -- n'est pas inventee.
    """
    from core.verification import clear_connection_health_red

    with health_row.cursor() as cur:
        cur.execute(
            "INSERT INTO app.connection_health (connection_ref_id, status, last_checked_at) "
            "VALUES (%s, 'revoked', now())",
            (CRED,),
        )
    clear_connection_health_red(CRED, evidence="account_verified:acct_EXAMPLE")

    status, pull_id, _, _ = _read(health_row)
    assert status == "revoked", "une lecture a ranime une autorisation revoquee"
    assert pull_id is None


def test_the_poller_keeps_the_label_with_the_sticky_red_and_drops_it_with_revoked(health_row):
    """LE POLLER EST LE SEUL A POUVOIR SE TROMPER DANS LES DEUX SENS.

    Il doit garder un `populate_failed` collant -- le sondage d'authentification ne
    sait rien des donnees -- donc il doit garder l'etiquette avec. Mais `revoked`
    l'emporte sur le collant, et la ou le statut part l'etiquette part aussi : elle
    decrirait un etat que la ligne ne porte plus.
    """
    from core.health_poller import _upsert_health
    from core.verification import _set_connection_health_red

    _set_connection_health_red(CRED, "empty", PULL_ONE)

    _upsert_health(CRED, "ok", datetime.now(tz=timezone.utc), None)
    status, pull_id, verdict, _ = _read(health_row)
    assert (status, pull_id, verdict) == ("populate_failed", PULL_ONE, "empty"), (
        "le sondage a efface le rouge de donnees ou son etiquette"
    )

    _upsert_health(CRED, "revoked", datetime.now(tz=timezone.utc), None)
    status, pull_id, verdict, raised_at = _read(health_row)
    assert status == "revoked"
    assert (pull_id, verdict, raised_at) == (None, None, None), (
        "l'etiquette du rouge de donnees survit a une revocation"
    )
