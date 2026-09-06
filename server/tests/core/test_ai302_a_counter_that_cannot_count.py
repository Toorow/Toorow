"""AI-302 -- un compteur qui ne sait pas compter n'est pas un zero.

LA CHAINE MESUREE EN PRODUCTION, le 2026-08-17 (et a l'identique le 2026-08-12) :

    09:57:52  job enqueue (audience by age and gender)
    09:58:17  344 lignes atterrissent dans raw_youtube_breakdown, avec CE pull_id
    09:58:24  verification: populate_failed  verdict=empty  pull_id=pull_01M07JFX...
    11:16:xx  9 x enqueue_refused code=access_denied

`app.connection_health.status = 'populate_failed'` est COLLANT, et la porte de
topologie ne laisse passer que `ok` ou `stale` : un seul verdict `empty` faux ferme
les NEUF flux actifs de l'autorisation. La cause immediate -- l'abonne
`facts_api.run_verification_for` ne passait pas `report_profile_id` -- est reparee
par le commit `70a90084`. Ce fichier ferme la CLASSE, en trois points que ce commit
laisse ouverts :

1. `_count_raw_rows` rendait `0` sur SIX chemins ou il n'avait rien lu : relation
   introuvable, table non enregistree, backend inconnu, BigQuery injoignable,
   fichier DuckDB absent, requete en erreur. `0` est ce qui leve le rouge collant.
   Un comptage qui n'a pas eu lieu rend desormais `None`, et aucun verdict n'est
   filé -- seul un `SELECT COUNT(*)` qui a reellement tourne peut dire 0.

2. Le rouge NOMME le tirage qui l'a pose (migration 276). La ligne ne disait que
   `populate_failed` : ni tirage, ni verdict, ni date propre -- `last_checked_at`
   est reecrit par chaque cycle du poller, donc il ne date pas le rouge.

3. Le refus cesse de sortir sous un code generique. `access_denied` recouvrait
   « vous n'avez pas le droit » et « la sante de cette connexion est rouge » : deux
   gestes de reparation opposes sous un seul mot.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

#: Le connecteur de la mesure : il declare DEUX relations brutes
#: (`raw_youtube_daily`, `raw_youtube_breakdown`), et c'est toute l'affaire.
MULTI_RELATION_MODULE = "youtube-analytics"
#: Le profil dont les 344 lignes sont parties dans `raw_youtube_breakdown`.
BREAKDOWN_PROFILE = "audience_demographics"
#: Un connecteur qui n'en declare qu'UNE : le repli du registre y reste legitime.
SINGLE_RELATION_MODULE = "gsc"

CONN_REF = "conn_ref_ai302"
PROJECT = "proj_EXAMPLE"
PULL = "pull_EXAMPLE302"


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """`TOOROW_RAW_TABLE_NAME` est une declaration explicite et court-circuite tout.

    Une session qui l'exporte ferait passer au vert des tests qui ne prouvent plus
    rien : la relation serait donnee, jamais resolue.
    """
    monkeypatch.delenv("TOOROW_RAW_TABLE_NAME", raising=False)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)


# ===========================================================================
# 1. Le compteur refuse de conclure quand il n'a pas lu
# ===========================================================================


def test_the_declared_relation_count_sees_the_two_relations_of_the_measure():
    """L'instrument d'abord : sans lui les trois tests suivants sont des opinions.

    39 connecteurs, 9 declarent plus d'une relation brute. Si ce compte tombe a 1
    pour youtube-analytics, la regle « ne pas deviner » ne se declenche plus et les
    tests ci-dessous passent au vert pour rien.
    """
    from core.verification import _declared_raw_relation_count

    # PLUS D'UNE, ET NON EXACTEMENT DEUX. Ce test epinglait `== 2` et il est
    # devenu rouge le 2026-09-01 sans qu'aucune regle ne bouge : fdb78ef2 a
    # ajoute `raw_youtube_video_directory` au connecteur, donc trois. Ce que
    # l'instrument doit garantir est ce que sa propre docstring dit -- que le
    # module de mesure declare PLUSIEURS relations, sinon la regle « ne pas
    # deviner » ne se declenche plus et les trois tests suivants passent au vert
    # pour rien. Un compte exact fait echouer chaque relation ajoutee au
    # connecteur, ce qui n'est pas une propriete du compteur.
    assert _declared_raw_relation_count(MULTI_RELATION_MODULE) > 1
    assert _declared_raw_relation_count(SINGLE_RELATION_MODULE) == 1


def test_no_profile_on_a_multi_relation_connector_counts_nothing(tmp_path, monkeypatch):
    """LE DEFAUT DU 2026-08-17, reduit a son os.

    L'abonne ne passait aucun `report_profile_id`. Le registre du module rend UNE
    table -- `raw_youtube_daily` -- alors que les 344 lignes etaient dans
    `raw_youtube_breakdown`. Le comptage etait donc un zero VRAI a propos de la
    mauvaise table, et rien dans les logs ne le disait : ni
    `raw_table_name not configured`, ni `profile_relation_unreadable`, ni
    `count_raw_rows_bigquery_failed`.

    La base DuckDB existe et porte les 344 lignes : si la fonction rendait 0 ici,
    ce serait bien parce qu'elle a regarde ailleurs, pas parce qu'elle n'a pas pu.
    """
    import duckdb
    from core import verification

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE raw_youtube_breakdown (pull_id VARCHAR)")
    con.executemany("INSERT INTO raw_youtube_breakdown VALUES (?)", [(PULL,)] * 344)
    con.execute("CREATE TABLE raw_youtube_daily (pull_id VARCHAR)")
    con.close()
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    verification.register_raw_table_name("raw_youtube_daily", provider=MULTI_RELATION_MODULE)

    counted = verification._count_raw_rows(PULL, MULTI_RELATION_MODULE, project_id=PROJECT)

    assert counted is None, (
        f"comptage={counted} -- sans profil, le registre ne peut nommer qu'une des deux "
        "relations du connecteur, et un zero pris dans la mauvaise ferme neuf flux"
    )


def test_the_profiles_own_relation_is_counted_when_it_is_named(tmp_path, monkeypatch):
    """Et le contre-test : nomme, le profil rend le VRAI compte.

    Sinon le test precedent serait satisfait par une fonction qui ne compte plus
    jamais rien.
    """
    import duckdb
    from core import verification

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE raw_youtube_breakdown (pull_id VARCHAR)")
    con.executemany("INSERT INTO raw_youtube_breakdown VALUES (?)", [(PULL,)] * 344)
    con.close()
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    verification.register_raw_table_name("raw_youtube_daily", provider=MULTI_RELATION_MODULE)

    counted = verification._count_raw_rows(
        PULL,
        MULTI_RELATION_MODULE,
        project_id=PROJECT,
        report_profile_id=BREAKDOWN_PROFILE,
    )

    assert counted == 344, f"comptage={counted}, attendu 344 dans raw_youtube_breakdown"


def test_a_single_relation_connector_still_uses_its_registry(tmp_path, monkeypatch):
    """LA REGLE NE PUNIT PAS LES 30 AUTRES CONNECTEURS.

    Un connecteur qui ne declare qu'une relation n'a pas de seconde table avec
    laquelle se tromper : son repli de registre reste la seule adresse qui existe,
    et le rendre `None` supprimerait la verification de 30 connecteurs sur 39.
    """
    import duckdb
    from core import verification

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE raw_gsc_daily (pull_id VARCHAR)")
    con.executemany("INSERT INTO raw_gsc_daily VALUES (?)", [(PULL,), (PULL,)])
    con.close()
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    verification.register_raw_table_name("raw_gsc_daily", provider=SINGLE_RELATION_MODULE)

    assert verification._count_raw_rows(PULL, SINGLE_RELATION_MODULE) == 2


def test_an_unregistered_table_counts_nothing_instead_of_zero(monkeypatch):
    """`raw_table_name not configured` rendait 0, donc verdict `empty`.

    C'est le chemin que la story-log nomme explicitement :
    `_get_raw_table_name("youtube-analytics")` rend VIDE quand le module n'a pas
    ete importe -- « donc un profil non resolu fait rendre 0 au compteur sans rien
    dire ». Rien dire ET rendre 0 est ce qui ferme la porte.
    """
    from core import verification

    monkeypatch.setattr(verification, "_raw_table_names", {})
    monkeypatch.setattr(verification, "_raw_table_fallback", "")

    assert verification._count_raw_rows(PULL, "a-module-that-registered-nothing") is None


def test_an_unknown_backend_counts_nothing_instead_of_zero(monkeypatch):
    """Un `TOOROW_DB_MODE` inconnu ne mesure rien -- il ne mesure pas zero."""
    from core import verification

    monkeypatch.setenv("TOOROW_DB_MODE", "clickhouse")
    verification.register_raw_table_name("raw_gsc_daily", provider=SINGLE_RELATION_MODULE)

    assert verification._count_raw_rows(PULL, SINGLE_RELATION_MODULE) is None


def test_a_missing_duckdb_file_counts_nothing_instead_of_zero(monkeypatch):
    """Le chemin exact du 2026-08-12 cote local : pas de fichier, donc pas de mesure."""
    from core import verification

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "")
    verification.register_raw_table_name("raw_gsc_daily", provider=SINGLE_RELATION_MODULE)

    assert verification._count_raw_rows(PULL, SINGLE_RELATION_MODULE) is None


# ===========================================================================
# 2. Aucun verdict, et surtout aucun rouge, sur un comptage qui n'a pas eu lieu
# ===========================================================================


def _run_verification(*, counted, verdict_sink: list, health_calls: list, cleared: list):
    """Lance `run_post_pull_verification` avec un comptage impose.

    Le temoin observable est ce qui PART vers la base : la fonction ne rend rien et
    ne leve jamais (HG-1).
    """
    from core import verification

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "pull_verifications" in " ".join(str(sql).split()):
                verdict_sink.append(params)

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    @contextmanager
    def _get_connection():
        yield _Conn()

    with (
        patch("core.db.get_connection", new=_get_connection),
        patch.object(verification, "_count_raw_rows", return_value=counted),
        patch.object(
            verification,
            "_set_connection_health_red",
            side_effect=lambda *a, **k: health_calls.append((a, k)),
        ),
        patch.object(
            verification,
            "clear_connection_health_red",
            side_effect=lambda *a, **k: cleared.append((a, k)),
        ),
    ):
        verification.run_post_pull_verification(
            pull_id=PULL,
            connection_ref_id=CONN_REF,
            date_from="2026-07-18",
            date_to="2026-08-16",
            manifest={},
            provider=MULTI_RELATION_MODULE,
            project_id=PROJECT,
            report_profile_id=BREAKDOWN_PROFILE,
        )


def test_an_uncounted_pull_files_no_verdict_and_raises_no_red(caplog):
    """LE COEUR DE LA REPARATION.

    Un comptage impossible n'ecrit aucune ligne de verification -- chaque colonne
    de `app.pull_verifications` est une mesure, et il n'y en a pas -- et surtout ne
    leve AUCUN rouge. C'est le rouge, pas la ligne, qui a ferme neuf flux.

    LE LOG FAIT PARTIE DE L'AFFIRMATION, et pas par gout du detail. Sans profil, le
    code d'avant tombait sur `round(None / expected)` : un `TypeError` avale par le
    `except` de la fonction, donc lui aussi n'ecrivait rien et ne levait rien -- par
    ACCIDENT, sous le message `run_post_pull_verification_failed`, indistinguable
    d'une panne. Un refus deliberé se nomme : `unverified`, avec le tirage, le
    module et le profil. C'est l'instrument qui a permis de remonter les cinq jours
    de silence d'AI-301 en quelques secondes.
    """
    import logging

    rows: list = []
    reds: list = []
    cleared: list = []

    with caplog.at_level(logging.WARNING, logger="core.verification"):
        _run_verification(counted=None, verdict_sink=rows, health_calls=reds, cleared=cleared)

    assert rows == [], f"un verdict a ete file sans mesure : {rows}"
    assert reds == [], "un rouge COLLANT a ete leve sur un comptage qui n'a pas eu lieu"
    assert cleared == [], "et rien n'est efface non plus : on ne sait rien"

    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "unverified" in said, f"le refus de conclure n'est pas dit : {said!r}"
    assert PULL in said and BREAKDOWN_PROFILE in said, (
        f"le message ne nomme pas le tirage et son profil : {said!r}"
    )
    assert "run_post_pull_verification_failed" not in said, (
        "le silence vient d'une exception avalee, pas d'un refus deliberé : " + said
    )


def test_a_measured_zero_still_files_empty_and_still_raises_the_red():
    """LE CONTRE-TEST, sans lequel le precedent serait satisfait par du silence.

    Un `SELECT COUNT(*)` qui a tourne et rendu 0 est une mesure : le tirage est
    vide, le verdict est `empty`, et le rouge doit se lever. AI-302 ne desarme pas
    la verification, il lui interdit de conclure sans avoir lu.
    """
    rows: list = []
    reds: list = []
    cleared: list = []

    _run_verification(counted=0, verdict_sink=rows, health_calls=reds, cleared=cleared)

    assert len(rows) == 1, f"aucun verdict sur un zero MESURE : {rows}"
    assert "empty" in rows[0], f"verdict attendu 'empty', ligne={rows[0]}"
    assert len(reds) == 1, "un tirage reellement vide doit lever le rouge"
    assert reds[0][0][2] == PULL, f"le rouge ne nomme pas le tirage : {reds[0]}"


def test_a_verified_pull_lifts_the_red_the_bad_one_raised():
    """L'ECRITURE SYMETRIQUE QUE LA DOCSTRING PROMETTAIT ET QUE PERSONNE N'APPELAIT.

    `clear_connection_health_red` dit d'elle-meme etre « cleared by verification
    writing 'ok' after a successful verified pull ». Son seul appelant etait la
    verification de compte : un rouge pose par une fenetre vide survivait donc a
    toutes les bonnes fenetres suivantes, et seule une re-verification manuelle du
    compte pouvait le lever.
    """
    rows: list = []
    reds: list = []
    cleared: list = []

    _run_verification(counted=344, verdict_sink=rows, health_calls=reds, cleared=cleared)

    assert len(rows) == 1 and "ok" in rows[0], f"verdict attendu 'ok', ligne={rows[0]}"
    assert reds == [], "un tirage plein ne leve aucun rouge"
    assert len(cleared) == 1, "un tirage verifie doit LEVER le rouge, pas seulement ne pas le poser"
    assert PULL in cleared[0][1]["evidence"], (
        f"la preuve du deverrouillage ne nomme pas le tirage : {cleared[0]}"
    )


# ===========================================================================
# 2b. A partial landed rows, so it never closes the door (AI-101, 2026-08-17)
# ===========================================================================
#
# The window here is 30 days (2026-07-18 -> 2026-08-16) with an empty manifest,
# so `compute_expected_rows` answers 30 -- one row per day, the safe minimum.
# Four rows counted is a ratio of 0.13, below the 0.5 threshold: `partial`.


def test_a_partial_files_its_verdict_and_never_raises_the_sticky_red():
    """THE SUSPENSION THE SCHEDULE DECISION REFUSES, ARRIVING THROUGH THE BACK DOOR.

    `populate_failed` is sticky and the enqueue gate admits only `ok` or `stale`,
    so raising it on `verdict == "partial"` shut every Datastream behind one
    credential -- on a draw at 13% of its expectation, which is to say on a pull
    THAT HAD LANDED ROWS. AI-101 already settled that "Retrieval is never stopped
    by emptiness"; sparseness is a weaker reason still.

    What the partial keeps is its verdict: the line lands in
    `app.pull_verifications` exactly as before (HG-1 -- a verification is an
    annotation, never job control), so the day axis, the streaks and the
    re-collections all still see it. A quiet fact plus an alert, not a closed door.
    """
    rows: list = []
    reds: list = []
    cleared: list = []

    _run_verification(counted=4, verdict_sink=rows, health_calls=reds, cleared=cleared)

    assert len(rows) == 1 and "partial" in rows[0], (
        f"the partial verdict must still be filed, line={rows[0]}"
    )
    assert reds == [], (
        "a sparse window raised the STICKY red, which closes every Datastream of "
        "the authorization -- exactly the suspension AI-101 refuses to implement"
    )


def test_a_partial_does_not_lift_a_red_either():
    """AND IT DOES NOT RE-OPEN THE DOOR, WHICH IS THE OTHER HALF OF THE RULE.

    The ratified sentence is "A verified pull lifts the red" of the `ok` verdict
    alone. A partial proves the window renders SOMETHING; it does not prove the
    collection is healthy. Lifting on it would clear a red raised by a genuinely
    empty window on evidence too thin to have raised one.
    """
    rows: list = []
    reds: list = []
    cleared: list = []

    _run_verification(counted=4, verdict_sink=rows, health_calls=reds, cleared=cleared)

    assert cleared == [], (
        "a partial lifted an existing red -- 13% of a window is not proof the "
        "collection recovered"
    )


# ===========================================================================
# 3. Deux gestes de reparation, donc deux codes
# ===========================================================================


def _refusal_with_health(row):
    """`_topology_scope_refusal` sur une porte fermee par la SANTE, avec cette ligne.

    Le curseur repond par requete et jamais par position : un `side_effect=[...]`
    epinglerait le test au nombre exact de lectures du garde le jour ou il est
    ecrit (defaut deja consigne dans `test_epic36_queue_gate.py`).
    """
    from core import account_topology, db, project_access, queue

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False

    def _answer():
        sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list[-1:])
        if "connection_health" in sql:
            return row
        if "connection_account_scope" in sql or "credential_accounts" in sql:
            return ("acct-1",)
        return ("org-1",)

    cur.fetchone.side_effect = _answer
    # 2026-08-30: the credential-wide fallback COUNTS its candidates (`fetchall`)
    # instead of taking the freshest one, so a fixture has to say how many there
    # are. One ready account of this connector -- no ambiguity, and this subject
    # is about the access decision that follows, not about the count.
    cur.fetchall.return_value = [("acct-1", "google-ads")]
    conn.cursor.return_value = cur

    with (
        patch.object(db, "get_connection", lambda: nullcontext(conn)),
        patch.object(db, "request_connection", lambda _i: nullcontext(conn)),
        patch.object(db, "background_connection", lambda _r: nullcontext(conn)),
        patch.object(db, "set_local_access_context", MagicMock()),
        patch.object(
            queue,
            "_resolve_connection_ref",
            lambda _conn, _id: {"provider": "google-ads", "project_id": "proj-1"},
        ),
        patch.object(account_topology, "get_topology_for_provider", lambda _p: {}),
        patch.object(account_topology, "is_account_ready", lambda _i, _a, _c: True),
        patch.object(
            project_access,
            "resolve_provider_account_access",
            MagicMock(return_value=project_access.AccessDecision(False, "connection_unhealthy")),
        ),
    ):
        return queue._topology_scope_refusal(CONN_REF, requested_by="member-1", datastream_id=None)


def test_a_red_health_no_longer_refuses_under_the_permission_code(monkeypatch):
    """LES NEUF `access_denied` DU 2026-08-17, et pourquoi ils etaient illisibles.

    Le meme mot recouvrait « pas d'autorisation » et « sante rouge ». L'operateur
    a lu un probleme de droits pendant cinq jours, alors que le geste qui reparait
    etait de regarder une COLLECTE.
    """
    from datetime import datetime, timezone

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    from core.queue import REFUSAL_CONNECTION_UNHEALTHY

    refusal = _refusal_with_health(
        ("populate_failed", "pull_01M07JFX", "empty", datetime(2026, 8, 17, tzinfo=timezone.utc))
    )

    assert refusal is not None
    assert refusal["code"] == REFUSAL_CONNECTION_UNHEALTHY, (
        f"code={refusal['code']} -- une sante rouge et un droit manquant sont deux "
        "gestes de reparation, donc deux codes"
    )
    assert "pull_01M07JFX" in refusal["message"], (
        f"le refus ne nomme pas la collecte qui a ferme la porte : {refusal['message']}"
    )
    assert "2026-08-17" in refusal["message"], (
        f"le refus ne date pas le rouge : {refusal['message']}"
    )
    # Le geste, pas la cause technique : ni le nom de la colonne, ni celui du statut.
    assert "populate_failed" not in refusal["message"]
    assert "connection_health" not in refusal["message"]


def test_a_dead_authorization_names_reconnecting_and_not_a_collection(monkeypatch):
    """`revoked` et `populate_failed` ferment la meme porte et se reparent autrement.

    Aucune collecte ne ranime une autorisation morte : la phrase envoie reconnecter,
    et ne parle d'aucun tirage.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    from core.queue import REFUSAL_CONNECTION_UNHEALTHY

    refusal = _refusal_with_health(("revoked", None, None, None))

    assert refusal["code"] == REFUSAL_CONNECTION_UNHEALTHY
    assert "reconnect" in refusal["message"].lower(), refusal["message"]
    assert "(collection" not in refusal["message"], (
        f"la phrase pointe une collecte precise alors qu'aucune n'est en cause : "
        f"{refusal['message']}"
    )


def test_a_never_checked_connection_says_so_rather_than_inventing_a_pull(monkeypatch):
    """Pas de ligne de sante du tout : la phrase ne peut nommer aucun tirage."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    from core.queue import REFUSAL_CONNECTION_UNHEALTHY

    refusal = _refusal_with_health(None)

    assert refusal["code"] == REFUSAL_CONNECTION_UNHEALTHY
    assert "refresh the connection" in refusal["message"].lower()


def test_a_missing_right_still_refuses_without_disclosing_which_one(monkeypatch):
    """LA MOITIE QUI NE BOUGE PAS, et c'est deliberé.

    Nommer laquelle des quatre conditions de droit manque repondrait a une question
    dont l'appelant n'a pas prouve qu'il peut la poser. Seule la sante -- l'etat de
    SA propre connexion, deja affiche dans la Flotte -- se divulgue.
    """
    from core import account_topology, db, project_access, queue

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = lambda: ("acct-1",)
    # Un seul compte pret pour ce connecteur : la selection est sans ambiguite,
    # et ce qui est en cause ici est le DROIT qui vient apres (2026-08-30).
    cur.fetchall.return_value = [("acct-1", "google-ads")]
    conn.cursor.return_value = cur

    with (
        patch.object(db, "request_connection", lambda _i: nullcontext(conn)),
        patch.object(db, "set_local_access_context", MagicMock()),
        patch.object(
            queue,
            "_resolve_connection_ref",
            lambda _conn, _id: {"provider": "google-ads", "project_id": "proj-1"},
        ),
        patch.object(account_topology, "get_topology_for_provider", lambda _p: {}),
        patch.object(account_topology, "is_account_ready", lambda _i, _a, _c: True),
        patch.object(
            project_access,
            "resolve_provider_account_access",
            MagicMock(
                return_value=project_access.AccessDecision(False, "account_exposure_required")
            ),
        ),
    ):
        refusal = queue._topology_scope_refusal(
            CONN_REF, requested_by="member-1", datastream_id=None
        )

    assert refusal == {
        "state": "refused",
        "code": "access_denied",
        "message": "connection/account scope is not authorized for this resource",
    }
