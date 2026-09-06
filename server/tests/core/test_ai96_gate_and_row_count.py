"""AI-96 [1] et [3] -- la porte d'enfilement et le comptage qui la nourrit.

Deux constats de la marche statique du 2026-07-31, mesures ici plutot qu'affirmes.

[1] `_topology_scope_refusal` interroge la topologie sous le nom du CREDENTIAL.
    Un consentement Google direct est stocke `provider='google'`, qui n'est le nom
    d'aucun module : `get_topology_for_provider('google')` rend None, et la porte
    de la story 46.4 -- fail-closed, et c'est juste -- refuse TOUT pull Google.
    C'est la seule famille de connecteurs qui sache s'authentifier en production.

    AI-95 avait corrige les huit consommateurs de `_execute_job`. Celui-ci vit
    dans `_topology_scope_refusal`, qui tourne a l'ENFILEMENT et n'etait pas dans
    ce lot : le job est refuse avant d'exister, donc `_execute_job` ne le voit
    jamais et son garde ne pouvait pas l'attraper.

[3] `_count_raw_rows` compte dans DuckDB, toujours. En mode BigQuery il n'y a
    aucun fichier DuckDB, donc la fonction retombe sur `return 0` avec un simple
    WARNING : `actual_rows` vaut 0 PAR CONSTRUCTION en production, tout pull sort
    en verdict 'empty', et la chaine se ferme (pas de candidat pret ->
    `datastream_publication.py:398` bloque -> pas d'`active` -> pas de nightly).
    DuckDB reste le backend de test local (directive Jean 2026-07-30) : les tests
    ci-dessous epinglent AUSSI qu'il continue de compter la ou il ecrit.

La regle heritee d'AI-95 tient dans les deux sens et elle est testee ici aussi :
nom du MODULE partout ou l'on cherche un manifeste, un catalogue, une topologie
ou une table brute ; nom du CREDENTIAL seulement dans la tracabilite.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from tests.support.statement_router import (  # noqa: E402
    StatementInventory,
    UnknownStatement,
    describe,
)

#: L'autorisation telle qu'elle est stockee pour un consentement Google direct.
CREDENTIAL = "google"
#: L'outil que ce datastream lit reellement -- un module qui existe sur le disque.
MODULE = "gsc"

CONN_REF = "conn_ref_ai96"
DATASTREAM = "ds_ai96"
PROJECT = "proj_EXAMPLE"


# ===========================================================================
# [1] La porte d'enfilement
# ===========================================================================


# EVERY STATEMENT THE ENQUEUE GATE ISSUES, named. Measured by replaying
# `_topology_scope_refusal` with a recording cursor (AI-317), not read off the
# old `if/elif` chain -- which knew six of the thirteen. The seven it never
# modelled all fell into an `else` that answered "no row":
#
#   * the identity translation (`identity_bridge.py:82`), which also calls
#     `fetchall()` -- a method this fake did not have. The AttributeError was
#     swallowed by that function's own `except Exception` and logged as
#     "unresolved subject"; the fixture had a broken cursor and said nothing;
#   * the four statements `db.install_access_context` runs to arm the RLS floor
#     (`db.py:201/205/166/167`) and the read-back that refuses a connection which
#     forgot its context (`db.py:281`). The read-back has an explicit escape
#     hatch for a double that does not script it, so the whole floor was skipped;
#   * THE ONE THAT MATTERS: `SELECT ca.external_account_id FROM app.datastreams d
#     JOIN app.credential_accounts ca ...` (`queue.py:833`), the Datastream's own
#     account binding. Its `None` IS a decision -- "this Datastream binds no
#     account, fall back to the credential-wide scope" -- and the gate then reads
#     an account the test never chose. Migration 211 moved that binding onto
#     `app.datastreams` precisely so the two could differ.
#
# Declaration order is first match wins.
_ENQUEUE_GATE = StatementInventory(
    "_RoutingCursor (queue._topology_scope_refusal)",
    person_identity="from app.person_identities",
    current_user="select current_user",
    set_role="set role",
    set_identity="set_config('toorow.identity'",
    arm_the_floor="set_config('toorow.enforce_epic36'",
    access_context_readback="current_setting('toorow.enforce_epic36'",
    connection_ref="from app.connection_ref",
    datastream_module=("select module_name", "from app.datastreams"),
    # 2026-08-30: the credential-wide fallback is narrowed to ONE connector and
    # reads every candidate, so its projection and its FROM both moved. Named,
    # not widened: the account-exact probe above must keep winning.
    connector_ready_accounts=(
        "select s.account_id, ca.discovered_for_connector",
        "from app.connection_account_scope s",
    ),
    datastream_account="join app.credential_accounts",
    ready_scope_for_account=(
        "from app.connection_account_scope",
        "account_id = %s and state = %s",
    ),
    datastream_org=("select org_id", "from app.datastreams"),
    project_org=("select org_id", "from app.projects"),
)

#: The projections `describe` refuses to derive -- a function call is not a
#: column list, and a router that guessed one would be parsing SQL. Named here,
#: once, exactly where the derivation stops. Everywhere else the description
#: comes from the statement itself, so a moved projection cannot go on agreeing
#: with a hand-written tuple nothing reads.
_NAMED_DESCRIPTION = {
    "current_user": [("current_user",)],
    "set_identity": [("set_config",)],
    "arm_the_floor": [("set_config",)],
    "access_context_readback": [("current_setting",), ("current_setting",)],
}

#: The only statement here that returns NO RESULT SET. `description = None` is
#: what psycopg reserves for exactly that, and for nothing else.
_NO_RESULT_SET = frozenset({"set_role"})


class _RoutingCursor:
    """Repond selon la requete : c'est l'ecart entre `connection_ref.provider`
    et `datastreams.module_name` qui est en cause, donc les deux doivent differer.

    AI-317 : et il LEVE sur ce qu'on ne lui a pas appris. Attention en lisant un
    rouge -- `_topology_scope_refusal` enveloppe tout son corps dans un
    `except Exception` (queue.py:2977) qui rend un refus `access_denied`. Un
    `UnknownStatement` leve pendant le parcours ressort donc en refus, pas en
    trace : c'est ce refus inattendu qu'il faut lire comme « une requete a bouge ».
    """

    def __init__(
        self,
        *,
        module_name: str | None = MODULE,
        ready_scope: bool = True,
        session: dict | None = None,
    ):
        self._row = None
        self._module_name = module_name
        self._ready_scope = ready_scope
        # Les GUC de la SESSION, portes par la connexion et non par le curseur :
        # `install_access_context` les pose sur un curseur puis les relit sur un
        # autre, et une doublure qui les oublie fait tomber le plancher entre les
        # deux. Ici la connexion garde son contexte, donc la garde de db.py:281
        # mesure vraiment quelque chose au lieu de prendre son echappatoire.
        self._session = {} if session is None else session
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        statement = _ENQUEUE_GATE.match(sql)
        if statement in _NO_RESULT_SET:
            self.description = None
        else:
            self.description = _NAMED_DESCRIPTION.get(statement) or describe(sql)
        args = tuple(params or ())
        match statement:
            case "person_identity":
                # Aucun `person_identities` pour ce sujet : `canonical_identity`
                # lit zero ligne et rend l'identite inchangee (identity_bridge.py:94).
                self._row = None
            case "current_user":
                # La production se connecte en `postgres` et DEVIENT `connector`
                # juste apres ; c'est ce chemin-la que le SET ROLE ci-dessous suit.
                self._row = (self._session.get("role", "postgres"),)
            case "set_role":
                self._row = None
            case "set_identity":
                self._session["toorow.identity"] = args[0] if args else None
                self._row = (self._session["toorow.identity"],)
            case "arm_the_floor":
                self._session["toorow.enforce_epic36"] = "on"
                self._row = ("on",)
            case "access_context_readback":
                self._row = (
                    self._session.get("toorow.enforce_epic36"),
                    self._session.get("toorow.identity"),
                )
            case "connection_ref":
                self._row = (CONN_REF, "nango-ai96", CREDENTIAL, PROJECT)
            case "datastream_module":
                self._row = (self._module_name,) if self._module_name else None
            case "datastream_account":
                # Ce Datastream ne porte AUCUNE liaison de compte : le join ne
                # rend rien et la garde retombe sur la portee de l'autorisation,
                # juste en dessous (queue.py:845). C'est ce que le fake repondait
                # deja -- par hasard, depuis son `else`.
                self._row = None
            case "ready_scope_for_account":
                self._row = (1,) if self._ready_scope else None
            case "connector_ready_accounts":
                # Un seul compte pret pour CE connecteur : le prendre n'est pas
                # un pari, c'est le seul que ce connecteur puisse lire ici. Le
                # sujet de ce fichier est l'identite du module a l'enqueue, pas
                # le denombrement -- deux comptes se mesurent ailleurs.
                self._row = ("acct_EXAMPLE", MODULE)
            case "datastream_org" | "project_org":
                self._row = ("org_EXAMPLE",)
            case _:  # pragma: no cover - un nom ajoute a l'inventaire, sans reponse
                raise _ENQUEUE_GATE.unknown(sql)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [] if self._row is None else [self._row]


class _RoutingConn:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        #: Les GUC survivent au curseur, comme sur une vraie session.
        self.session: dict = {}

    def cursor(self):
        return _RoutingCursor(session=self.session, **self._kwargs)

    def commit(self):
        pass

    def close(self):
        pass


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317 : un statement inconnu se NOMME, il ne repond pas « pas de ligne ».

    C'est la reponse que sept statements recevaient : celle qui, sur la liaison
    de compte du Datastream, veut dire « aucun compte lie » et fait basculer la
    garde sur la portee de l'autorisation. Une requete deplacee aurait continue
    de la produire.
    """
    cursor = _RoutingCursor()

    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT state FROM app.pull_jobs WHERE connection_ref_id = %s",
            (CONN_REF,),
        )

    message = str(raised.value)
    assert "app.pull_jobs" in message
    assert "datastream_account" in message


@contextmanager
def _conn_factory(**kwargs):
    yield _RoutingConn(**kwargs)


class _TopologyRecorder:
    """Capture le nom sous lequel la topologie a ete cherchee.

    Ne rend une topologie que pour un nom de MODULE reel -- exactement le
    comportement de `_resolve_module`, qui balaie le registre des modules
    charges et ne trouve rien sous 'google'.
    """

    def __init__(self, known=(MODULE,)):
        self.asked: list[str] = []
        self._known = set(known)

    def __call__(self, name):
        self.asked.append(name)
        return {"selection_level": "site"} if name in self._known else None


def _refusal(*, module_name=MODULE, datastream_id=DATASTREAM, known=(MODULE,)):
    """Lance `_topology_scope_refusal` et rend (refus, noms interroges)."""
    from core import queue

    recorder = _TopologyRecorder(known=known)

    @contextmanager
    def _get_connection():
        yield _RoutingConn(module_name=module_name)

    with (
        patch("core.db.get_connection", new=_get_connection),
        patch("core.db.set_local_access_context", lambda *a, **kw: None),
        patch(
            "core.account_topology.get_topology_for_provider",
            side_effect=recorder,
        ),
        patch("core.account_topology.has_ready_scope", return_value=True),
        patch(
            "core.project_access.resolve_provider_account_access",
            return_value=type("D", (), {"allowed": True})(),
        ),
    ):
        result = queue._topology_scope_refusal(
            CONN_REF,
            requested_by="user_EXAMPLE",
            datastream_id=datastream_id,
        )
    return result, recorder.asked


def test_the_topology_is_looked_up_under_the_module_not_the_credential():
    """LE CONSTAT [1], reduit a son os.

    Rouge avant reparation : la topologie est cherchee sous 'google'.
    """
    _, asked = _refusal()

    assert asked, "la topologie n'a jamais ete interrogee"
    assert set(asked) == {MODULE}, (
        f"topologie cherchee sous {set(asked)}, attendu {{'{MODULE}'}} -- "
        f"'{CREDENTIAL}' est le nom du credential, pas d'un module"
    )


def test_a_google_pull_is_not_refused_at_enqueue():
    """LA CONSEQUENCE VECUE : le clic sur « Run » rend `access_denied`.

    La porte fail-closed de la story 46.4 est correcte et reste inchangee ;
    ce qui change est le nom qu'on lui donne a resoudre.
    """
    result, _ = _refusal()

    assert result is None, (
        f"pull Google refuse a l'enfilement : {result} -- "
        "la porte a pris la confusion credential/module pour une absence de topologie"
    )


def test_a_connection_without_datastream_still_resolves_by_the_credential():
    """LE GARDE-FOU CONTRE LA SUR-CORRECTION, premier sens.

    Les 37 connecteurs Nango ont un credential qui EST le module, et le chemin
    par connexion (herite) n'a aucun datastream. Leur retirer ce nom les
    refuserait tous -- on echangerait un blocage contre un autre.
    """
    result, asked = _refusal(datastream_id=None, module_name=None, known=(CREDENTIAL,))

    assert asked == [CREDENTIAL], (
        f"sans datastream, la topologie doit etre cherchee sous le credential, vu {asked}"
    )
    assert result is None


def test_an_unknown_module_is_still_refused():
    """LE GARDE-FOU CONTRE LA SUR-CORRECTION, second sens : fail-closed tient.

    La story 46.4 refuse tout ce dont la topologie ne se resout pas. Reparer le
    NOM ne doit pas ouvrir la porte : un module qui n'existe nulle part reste
    un refus.
    """
    result, asked = _refusal(module_name="module_qui_nexiste_pas", known=(MODULE,))

    assert asked == ["module_qui_nexiste_pas"]
    assert result is not None and result.get("state") == "refused", (
        f"un module inconnu doit rester refuse, vu {result}"
    )


# ===========================================================================
# [3] Le comptage des lignes brutes
# ===========================================================================


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return iter(self._rows)


class _FakeBQClient:
    """Rend le compte demande et retient la requete -- on verifie qu'on a bien
    demande CE pull dans CETTE table, pas un COUNT(*) global."""

    project = "proj-example"

    def __init__(self, count=42):
        self.queries: list[str] = []
        self._count = count

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        return _FakeQueryJob([{"n": self._count}])


@pytest.fixture()
def bq(monkeypatch):
    from google.cloud import bigquery as real

    client = _FakeBQClient()
    monkeypatch.setattr(real, "Client", lambda *a, **kw: client)
    monkeypatch.setattr(
        "core.warehouse_tenancy.bigquery_raw_dataset",
        lambda pid, conn=None: "org_example_raw",
    )
    monkeypatch.setenv("GCP_PROJECT", "proj-example")
    monkeypatch.setenv("TOOROW_DB_MODE", "bigquery")
    # En production il n'y a AUCUN fichier DuckDB. C'est tout le constat.
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.delenv("TOOROW_RAW_TABLE_NAME", raising=False)
    return client


@pytest.fixture(autouse=True)
def _registered_raw_table():
    """Le module a enregistre sa table brute, comme au demarrage."""
    from core import verification

    verification.register_raw_table_name("raw_gsc_daily", MODULE)
    yield
    verification._raw_table_names.pop(MODULE, None)


def test_bigquery_mode_counts_in_bigquery(bq):
    """LE CONSTAT [3], reduit a son os.

    Rouge avant reparation : 0, sans qu'aucune requete ne parte -- la fonction
    cherche un fichier DuckDB qui n'existe pas en production et abandonne.
    """
    from core.verification import _count_raw_rows

    count = _count_raw_rows("pull_EXAMPLE", MODULE, project_id=PROJECT)

    assert bq.queries, (
        "aucune requete BigQuery : le comptage a cherche un fichier DuckDB "
        "qui n'existe pas en mode BigQuery, et a rendu 0 par construction"
    )
    assert count == 42, f"comptage rendu {count}, attendu 42"


def test_the_bigquery_count_is_scoped_to_this_pull_and_this_table(bq):
    """Un COUNT(*) global compterait l'historique entier du connecteur et
    rendrait tout pull 'ok', y compris un pull vide. Le filtre est le sujet."""
    from core.verification import _count_raw_rows

    _count_raw_rows("pull_EXAMPLE", MODULE, project_id=PROJECT)

    sql = bq.queries[0]
    assert "raw_gsc_daily" in sql, f"table absente de la requete : {sql}"
    assert "org_example_raw" in sql, f"dataset de l'org absent : {sql}"
    assert "pull_id" in sql, f"le comptage n'est pas borne au pull : {sql}"


#: Ordre des colonnes de l'INSERT dans `app.pull_verifications`
#: (verification.py, etape 5). Positionnel, pas nomme.
_VERIFICATION_COLUMNS = (
    "id",
    "pull_id",
    "connection_ref_id",
    "expected_rows",
    "actual_rows",
    "completeness_ratio",
    "verdict",
    "rejected_rows",
)


def _run_verification_capturing_the_row() -> dict:
    """Lance la verification et rend la LIGNE reellement ecrite.

    On lit ce qui part vers `app.pull_verifications`, pas ce que la fonction
    rend : `run_post_pull_verification` ne rend rien et ne leve jamais (HG-1),
    donc son seul temoin observable est cette ligne.
    """
    from core import verification

    written: list[dict] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "pull_verifications" in " ".join(str(sql).split()):
                written.append(dict(zip(_VERIFICATION_COLUMNS, params or ())))

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _get_connection():
        yield _Conn()

    with patch("core.db.get_connection", new=_get_connection):
        verification.run_post_pull_verification(
            pull_id="pull_EXAMPLE",
            connection_ref_id=CONN_REF,
            date_from="2026-07-01",
            date_to="2026-07-04",
            manifest={},
            provider=MODULE,
            project_id=PROJECT,
        )

    assert written, "aucune ligne de verification ecrite"
    return written[0]


def _verification_rows_written() -> list[dict]:
    """Comme ci-dessus, mais rend la LISTE -- y compris vide.

    AI-302 : un comptage qui n'a pas pu avoir lieu n'ecrit plus de ligne du tout,
    donc « zero ligne » est devenu une observation legitime que le helper au-dessus
    refuse par assertion. Les deux existent : celui-la observe l'absence, l'autre
    lit la ligne.
    """
    from core import verification

    written: list[dict] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "pull_verifications" in " ".join(str(sql).split()):
                written.append(dict(zip(_VERIFICATION_COLUMNS, params or ())))

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _get_connection():
        yield _Conn()

    with patch("core.db.get_connection", new=_get_connection):
        verification.run_post_pull_verification(
            pull_id="pull_EXAMPLE",
            connection_ref_id=CONN_REF,
            date_from="2026-07-01",
            date_to="2026-07-04",
            manifest={},
            provider=MODULE,
            project_id=PROJECT,
        )
    return written


def test_a_landed_pull_is_not_verdict_empty_in_bigquery_mode(bq):
    """LA CONSEQUENCE VECUE, bout en bout.

    C'est ce verdict qui ferme la chaine : `actual_rows=0` -> 'empty' -> pas de
    candidat pret -> `datastream_publication.py:398` bloque -> pas d'`active` ->
    pas de nightly.
    """
    row = _run_verification_capturing_the_row()

    assert row["actual_rows"] == 42, (
        f"actual_rows={row['actual_rows']} -- en mode BigQuery le comptage "
        "vaut 0 par construction tant qu'il passe par DuckDB"
    )
    assert row["verdict"] != "empty", (
        f"verdict={row['verdict']} sur un pull de 42 lignes reellement landees"
    )


def test_the_defect_itself_reproduced_reading_duckdb_where_bigquery_holds_the_rows(bq, monkeypatch):
    """LE ROUGE, GARDE EN PLACE : ce que faisait le code avant la reparation.

    Ce test ne verifie pas une regle produit, il PROUVE que le harnais ci-dessus
    distingue quelque chose. Il remet le lecteur sur DuckDB pendant que les
    lignes sont dans BigQuery -- exactement la situation de production avant
    AI-96 [3]. Sans lui, le test precedent pourrait passer au vert pour une raison
    qui n'a rien a voir, et personne ne le verrait.

    CE QU'IL CONSTATE A CHANGE AVEC AI-302, et le changement est le sujet. Il
    affirmait `actual_rows=0` et `verdict='empty'` -- les deux symptomes mesures.
    Le second etait le vrai degat : `empty` leve un `populate_failed` COLLANT qui
    refuse ensuite tout tirage de l'autorisation entiere. Un fichier DuckDB absent
    n'est pas un tirage vide, c'est un comptage qui n'a pas eu lieu, donc la
    fonction n'ecrit plus AUCUNE ligne et ne leve plus aucun rouge. Le harnais
    distingue toujours quelque chose : la reparation d'AI-96 se lit maintenant en
    « une ligne a 42 » contre « pas de ligne du tout ».
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")

    written = _verification_rows_written()

    assert written == [], (
        f"une ligne a ete ecrite sur un comptage qui n'a pas eu lieu : {written} -- "
        "un verdict 'empty' ici ferme les flux de toute l'autorisation"
    )
    assert not bq.queries, "BigQuery n'aurait pas du etre interroge en mode duckdb"


def test_duckdb_mode_is_untouched(monkeypatch, tmp_path):
    """LE GARDE-FOU : DuckDB reste le backend de TEST LOCAL et doit compter.

    Directive Jean 2026-07-30 : « DuckDB = local testing seulement ». Seulement
    ne veut pas dire plus jamais -- la recette locale compte la ou elle ecrit.
    """
    import duckdb
    from core.verification import _count_raw_rows

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE raw_gsc_daily (pull_id VARCHAR, value INTEGER)")
    con.executemany(
        "INSERT INTO raw_gsc_daily VALUES (?, ?)",
        [("pull_EXAMPLE", 1), ("pull_EXAMPLE", 2), ("pull_AUTRE", 3)],
    )
    con.close()

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    monkeypatch.delenv("TOOROW_RAW_TABLE_NAME", raising=False)

    assert _count_raw_rows("pull_EXAMPLE", MODULE) == 2


def test_an_unreachable_bigquery_still_never_raises(bq, monkeypatch):
    """HG-1 : la verification est une ANNOTATION, jamais un controle de job.

    `run_post_pull_verification` ne leve pas et n'echoue pas un pull. Ajouter un
    appel reseau ne doit pas transformer une panne BigQuery en pull rate.
    """
    from core.verification import _count_raw_rows

    def _boom(*a, **kw):
        raise RuntimeError("bigquery unreachable")

    monkeypatch.setattr(bq, "query", _boom)

    # AI-302 : et il rend `None`, pas 0. Un entrepot injoignable ne mesure rien ;
    # rendre 0 faisait filer un verdict `empty`, qui leve le `populate_failed`
    # collant et refuse ensuite tous les flux de l'autorisation. La panne ne doit
    # ni rater le pull (HG-1) ni le declarer vide.
    assert _count_raw_rows("pull_EXAMPLE", MODULE, project_id=PROJECT) is None
