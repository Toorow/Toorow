"""AI-95 -- le nom du CREDENTIAL ne doit jamais servir de nom de MODULE.

`app.connection_ref.provider` nomme l'autorisation, pas l'outil. Un consentement
Google direct est stocke `provider='google'`, qui n'est le nom d'AUCUN module :
le meme ecran ouvre Search Console, Analytics, Ads et quatre autres. Seul le
datastream sait lequel de ces outils ce job lit.

`_execute_job` l'avait corrige a UN endroit (la resolution de `pull_fn`) et
continuait a passer `provider` a huit autres consommateurs qui, eux, cherchent
un MODULE : le quota, le manifeste (deux fois), le catalogue, la route
`airbyte_sync`, la table brute de la verification, et l'amorce de contexte.

Ce que ca produisait, mesure en production : `_get_manifest_for_module` rend `{}`
quand le nom n'est pas un module, donc `compute_expected_rows` retombe sur son
repli « un jour = une ligne ». L'unique pull `done` de l'histoire du produit
porte `expected_rows=4` pour une fenetre de quatre jours -- l'instrument qui
jugeait le pull etait abime par le defaut qu'il mesurait.

La regle que ces tests epinglent, dans les deux sens :
  - nom du MODULE partout ou l'on cherche un manifeste, un catalogue, un quota
    de connecteur, une fonction de pull ou une table brute ;
  - nom du CREDENTIAL seulement dans la tracabilite (`provider_account=`).

Le second sens compte autant que le premier : une reparation qui remplacerait
`provider` partout ferait mentir les lignes d'audit.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tests.support.statement_router import StatementInventory, UnknownStatement, describe

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

#: L'autorisation telle qu'elle est stockee pour un consentement Google direct.
CREDENTIAL = "google"
#: L'outil que ce datastream lit reellement -- un module qui existe sur le disque.
MODULE = "gsc"


def _job() -> dict:
    return {
        "id": "job_ai95_01",
        "pull_id": "pull_ai95_01",
        "connection_ref_id": "conn_ref_ai95",
        "datastream_id": "ds_ai95",
        "date_from": "2026-07-01",
        "date_to": "2026-07-04",
        "requested_by": "test-user",
        "attempt_count": 0,
    }


#: Le profil de rapport que le datastream declare. Vide = chemin par defaut.
PROFILE_ID: str | None = None

#: Le compte que le Datastream LIE (`app.datastreams.source_account_id`, migration
#: 211). None = il n'en nomme aucun, ce que la 211 autorise explicitement.
DATASTREAM_ACCOUNT: str | None = None

#: Les comptes VERIFIES de ce consentement, tels que le repli les lit :
#: `(account_id, discovered_for_connector)`. Un consentement Google en porte
#: legitimement plusieurs, et ils n'appartiennent pas tous au meme connecteur.
READY_ACCOUNTS: list[tuple[str, str | None]] = []


#: AI-317 -- CHAQUE STATEMENT QU'UN RUN DE `_execute_job` EMET SUR CE FAUX
#: CURSEUR, NOMME. Il en emet onze ; l'ancien `execute()` en reconnaissait
#: TROIS et rendait `self._row = None` a tout le reste, sous un commentaire qui
#: en citait deux (« connection_account_scope, UPDATE pull_jobs, ... »).
#:
#: Les huit autres passaient donc sans un mot, et l'un d'eux coutait : les
#: bindings d'entites PUBLIES (`entity_bindings.list_bindings`) se lisent avec
#: `fetchall()`, que ce faux curseur n'avait pas -- `_tracked_entity_kwargs`
#: (`core/queue.py:1969-1997`) attrapait l'`AttributeError`, journalisait
#: `tracked_entity_drivers_unavailable` et ne passait AUCUNE identite au pull.
#: Une lacune de fixture portait le costume d'un refus produit.
#:
#: L'ordre de declaration est l'ordre d'emission, et il est signifiant : le
#: premier fragment qui matche gagne, et quatre de ces lectures visent
#: `app.datastreams`.
INVENTORY = StatementInventory(
    "_RoutingCursor",
    connection_ref=(
        "select id, nango_connection_id, provider, project_id",
        "from app.connection_ref",
    ),
    datastream_module=("select module_name", "from app.datastreams"),
    job_attempt=("update app.pull_jobs", "set attempt_count"),
    datastream_profile=("select report_profile_id", "from app.datastreams"),
    datastream_account=(
        "select ca.external_account_id",
        "join app.credential_accounts",
    ),
    # 2026-08-30 : le repli a l'echelle de l'autorisation est desormais BORNE au
    # connecteur et COMPTE ses candidats, donc sa projection et son FROM ont
    # bouge tous les deux. Le fragment d'avant (`select account_id`) ne matchait
    # plus rien, et le `except Exception` de `_resolve_selected_account` avalait
    # l'`UnknownStatement` en rendant None -- vert, sur un chemin non parcouru.
    connector_ready_accounts=(
        "select s.account_id, ca.discovered_for_connector",
        "from app.connection_account_scope s",
    ),
    entity_bindings="from app.datastream_entity_binding_versions",
    job_finish=("update app.pull_jobs", "set state = %s"),
    boundary_evidence_seen="select id from app.datastream_time_boundary_evidence",
    boundary_evidence_write="insert into app.datastream_time_boundary_evidence",
    datastream_record=("select ds.id", "from app.datastreams ds"),
)

#: Les statements qui ne rendent AUCUN jeu de resultat. `description = None` est
#: le signal que psycopg reserve a ceux-la, et a eux seuls.
_WRITES = frozenset({"job_attempt", "job_finish", "boundary_evidence_write"})


class _RoutingCursor:
    """Un curseur qui repond selon la requete, pas une ligne unique pour tout.

    Le harnais existant rend la meme ligne a toutes les requetes ; ici c'est
    justement l'ecart entre `connection_ref.provider` et `datastreams.module_name`
    qui est en cause, donc les deux doivent differer.

    AI-317 : et un statement jamais enseigne LEVE (`INVENTORY.match`), au lieu de
    rendre « pas de ligne » -- reponse qu'un vrai curseur ne donne qu'a une
    relation vide, jamais a une requete que personne n'a ecrite.
    """

    def __init__(self):
        self._row = None
        self._rows: list[tuple] = []
        self._statement: str | None = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        statement = INVENTORY.match(sql)
        self._statement = statement
        self._row = None
        self._rows = []
        # DERIVEE du statement, jamais recopiee : les quatre colonnes de la
        # reference de connexion n'etaient ecrites nulle part, et ce curseur
        # n'avait pas de `description` du tout -- `get_datastream` la lit.
        self.description = None if statement in _WRITES else describe(sql)
        if statement == "connection_ref":
            self._row = ("conn_ref_ai95", "nango-ai95", CREDENTIAL, "proj_ai95")
        elif statement == "datastream_module":
            self._row = (MODULE,)
        elif statement == "datastream_profile":
            self._row = (PROFILE_ID,)
        elif statement == "datastream_account":
            self._row = (DATASTREAM_ACCOUNT,) if DATASTREAM_ACCOUNT else None
        elif statement == "connector_ready_accounts":
            # Le faux applique la SEMANTIQUE du predicat, pas seulement sa
            # presence : `account_connector_sql` retient le connecteur demande
            # ET les lignes NULL (migration 210 : « inconnu », pas « d'un
            # autre »). Sans ca, ce fake rendrait le consentement entier et le
            # cas « un seul compte de ce connecteur » ne prouverait rien.
            # LU PAR SA FORME, JAMAIS PAR SA POSITION. Le predicat n'a plus
            # qu'un proprietaire depuis AI-327 --
            # `account_topology.ready_accounts_for_connector` -- et celui-la lie
            # l'etat en parametre : la liste des connecteurs a glisse de la
            # position 1 a la position 2. Lire l'index, c'etait tenir une copie
            # de la signature, et cette copie rendait ce fake muet sans un mot.
            wanted = next(
                (value for value in (params or ()) if isinstance(value, (list, tuple))),
                [],
            )
            self._rows = [
                row for row in READY_ACCOUNTS if row[1] is None or row[1] in wanted
            ]
        # Les autres lectures rendent HONNETEMENT rien, et chacune veut dire
        # quelque chose de precis dans cette fixture :
        #   datastream_account / connector_ready_accounts -- aucun compte n'a
        #     ete choisi ni verifie, donc le pull n'en recoit pas ;
        #   entity_bindings -- aucun binding publie (`self._rows = []`), ce qui
        #     est la seule reponse qui ne change pas le pipeline ;
        #   boundary_evidence_seen -- aucune preuve identique deja stockee,
        #     donc l'INSERT qui suit a lieu ;
        #   datastream_record -- ce sujet-ci (l'identite du module) ne modelise
        #     pas la fiche complete du Datastream ; sans elle les trois
        #     enregistrements de gouvernance ne s'executent pas, et aucun test
        #     de ce fichier ne parle d'eux.

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _RoutingConn:
    def cursor(self):
        return _RoutingCursor()

    def commit(self):
        pass

    def close(self):
        pass


@contextmanager
def _fake_get_connection():
    yield _RoutingConn()


@contextmanager
def _declared_profile(profile_id: str | None):
    """Le datastream declare *profile_id* le temps du bloc."""
    global PROFILE_ID
    previous = PROFILE_ID
    PROFILE_ID = profile_id
    try:
        yield
    finally:
        PROFILE_ID = previous


@contextmanager
def _consent(*, bound: str | None, ready: list[tuple[str, str | None]]):
    """Ce que ce consentement a verifie, et ce que le Datastream en nomme."""
    global DATASTREAM_ACCOUNT, READY_ACCOUNTS
    previous = (DATASTREAM_ACCOUNT, READY_ACCOUNTS)
    DATASTREAM_ACCOUNT, READY_ACCOUNTS = bound, list(ready)
    try:
        yield
    finally:
        DATASTREAM_ACCOUNT, READY_ACCOUNTS = previous


def _pull_fn(connection_id=None, date_from=None, date_to=None, project_id=None, pull_id=None):
    return {"pull_id": "pull_ai95_01", "row_count": 7}


class _Recorder:
    """Capture le nom passe a chaque consommateur module-dependant."""

    def __init__(self, manifest: dict | None = None):
        self.manifest_names: list[str | None] = []
        self.quota_read_cost: list[str | None] = []
        self.quota_pre_check: list[str | None] = []
        self.quota_spend: list[str | None] = []
        self.catalog_names: list[str | None] = []
        self.dispatch_names: list[str | None] = []
        self.verification_names: list[str | None] = []
        self.seed_names: list[list[str]] = []
        self.audit_accounts: list[str | None] = []
        self._manifest = manifest if manifest is not None else {}

    # -- les consommateurs qui doivent recevoir le MODULE ------------------ #
    def manifest_for(self, name):
        self.manifest_names.append(name)
        return dict(self._manifest)

    def get_read_cost(self, name):
        self.quota_read_cost.append(name)
        return 1

    def pre_check(self, name, points, *a, **kw):
        self.quota_pre_check.append(name)
        return True, "ok"

    def record_spend(self, name, points, *a, **kw):
        self.quota_spend.append(name)

    def catalog_selection(self, name, capability_report, job, conn=None):
        # `conn` since 2026-08-17: the resolver reads the Datastream's plan for
        # its field selection (`app.datastream_plan_versions`), so it needs the
        # connection the job is already being executed under. This recorder only
        # watches WHICH MODULE is asked for, so it ignores the connection -- but
        # it has to accept it, or the call raises TypeError and the job fails as
        # `unclassified`, which is how this suite first reported the change.
        self.catalog_names.append(name)
        return None

    def dispatch_pull(self, *, module_name, **kwargs):
        self.dispatch_names.append(module_name)
        return {"row_count": 7}

    def run_verification(self, **kwargs):
        self.verification_names.append(kwargs.get("provider"))

    def seed(self, project_id, module_names=None, **kwargs):
        self.seed_names.append(list(module_names or []))

    # -- le consommateur qui doit recevoir le CREDENTIAL ------------------- #
    def audit(self, **kwargs):
        self.audit_accounts.append(kwargs.get("provider_account"))


def _run(recorder: _Recorder):
    from core import quota

    with (
        patch("core.db.get_connection", new=_fake_get_connection),
        patch("core.main.get_module_pull_fn", return_value=_pull_fn),
        patch("core.queue._get_manifest_for_module", side_effect=recorder.manifest_for),
        patch("core.queue._resolve_catalog_selection", side_effect=recorder.catalog_selection),
        patch.object(quota, "get_read_cost", side_effect=recorder.get_read_cost),
        patch.object(quota, "pre_check", side_effect=recorder.pre_check),
        patch.object(quota, "record_spend", side_effect=recorder.record_spend),
        patch("core.loader.dispatch_pull", side_effect=recorder.dispatch_pull),
        patch(
            "core.verification.run_post_pull_verification",
            side_effect=recorder.run_verification,
        ),
        patch(
            "core.context_seed.seed_project_context_best_effort",
            side_effect=recorder.seed,
        ),
        patch("core.audit.write_audit_row", side_effect=recorder.audit),
    ):
        from core.queue import _execute_job

        return _execute_job(_job())


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317 : le faux curseur ne peut plus repondre a une question jamais posee.

    « Pas de ligne » est la reponse d'une relation vide, pas celle d'une requete
    que la fixture n'a jamais vue. Tant que les deux se disaient pareil, une
    requete deplacee dans `queue.py` restait verte ici en mesurant un chemin que
    le test ne parcourait plus.

    Le message doit porter les deux moitiees de la reparation : le statement
    refuse (« quelle requete a bouge ? ») et l'inventaire (« a quoi
    ressemblait-elle ? »).
    """
    untaught = "SELECT quota_remaining FROM app.connector_quotas WHERE module_name = %s"

    with pytest.raises(UnknownStatement) as raised:
        _RoutingCursor().execute(untaught)

    message = str(raised.value)
    assert "app.connector_quotas" in message, message
    assert "datastream_module" in message, message


def test_quota_is_keyed_by_module_not_credential():
    """Le budget d'un connecteur est enregistre sous le nom du module.

    `core/loader.py:542` enregistre la quota sous `manifest['name']`. Interroge
    avec 'google', l'engine ne connait pas la plateforme : `read_cost` vaut 0,
    `pre_check` rend « no_quota » -- donc les sept outils Google echappaient
    entierement au budget, sans qu'aucun log ne le dise.
    """
    rec = _Recorder()
    _run(rec)

    assert rec.quota_read_cost, "get_read_cost n'a jamais ete appele"
    assert set(rec.quota_read_cost) == {MODULE}, (
        f"quota interrogee sous {set(rec.quota_read_cost)}, attendu {{'{MODULE}'}}"
    )
    assert set(rec.quota_pre_check) == {MODULE}
    assert set(rec.quota_spend) == {MODULE}


def test_manifest_lookups_use_the_module():
    """Les deux lectures de manifeste decident l'attente de lignes et l'atterrissage.

    Celle de la ligne 1055 alimente `compute_expected_rows` : un manifeste vide
    y fait rendre « un jour = une ligne », ce qui est exactement l'`expected_rows=4`
    observe en production sur une fenetre de quatre jours.
    """
    rec = _Recorder()
    _run(rec)

    assert len(rec.manifest_names) >= 2, (
        f"attendu au moins deux lectures de manifeste, vu {rec.manifest_names}"
    )
    assert set(rec.manifest_names) == {MODULE}, (
        f"manifeste cherche sous {set(rec.manifest_names)}, attendu {{'{MODULE}'}}"
    )


def test_catalog_selection_uses_the_module():
    """Le catalogue est un repertoire sur le disque : `modules/<module>/api_catalog.json`."""
    rec = _Recorder()
    _run(rec)

    assert set(rec.catalog_names) == {MODULE}


def test_verification_raw_table_uses_the_module():
    """La table brute est enregistree par module (`verification._raw_table_names`).

    Sous un nom inconnu, `_get_raw_table_name` retombe sur le fallback global --
    c'est-a-dire la table d'un AUTRE connecteur : un comptage croise, rendu en
    verdict.
    """
    rec = _Recorder()
    _run(rec)

    assert rec.verification_names == [MODULE], (
        f"verification appelee avec {rec.verification_names}, attendu ['{MODULE}']"
    )


def test_context_seed_uses_the_module():
    """`seed_project_context_best_effort` filtre par nom de module (story 44.2)."""
    rec = _Recorder()
    _run(rec)

    assert rec.seed_names == [[MODULE]], (
        f"amorce de contexte appelee avec {rec.seed_names}, attendu [['{MODULE}']]"
    )


def test_airbyte_route_dispatches_the_module():
    """La route `airbyte_sync` reintroduisait le bug repare soixante lignes plus haut.

    `dispatch_pull(module_name=...)` cherche la fonction de pull dans le registre
    des modules charges -- exactement la resolution que la ligne 900 venait de
    corriger, et qui repartait en 'google' des que le profil actif declare
    `extraction_path: airbyte_sync`.
    """
    rec = _Recorder(
        manifest={
            "report_profiles": [{"id": "standard_daily", "extraction_path": "airbyte_sync"}]
        }
    )
    with _declared_profile("standard_daily"):
        _run(rec)

    assert rec.dispatch_names == [MODULE], (
        f"dispatch_pull appele avec {rec.dispatch_names}, attendu ['{MODULE}']"
    )


def test_audit_still_names_the_credential():
    """Le garde-fou contre la sur-correction.

    Les lignes d'audit tracent QUELLE AUTORISATION a servi. Y ecrire le module
    effacerait la seule trace du credential utilise -- et la trace est justement
    ce a quoi elle sert.
    """
    rec = _Recorder()
    _run(rec)

    assert rec.audit_accounts, "aucune ligne d'audit ecrite"
    assert set(rec.audit_accounts) == {CREDENTIAL}, (
        f"audit ecrit sous {set(rec.audit_accounts)}, attendu {{'{CREDENTIAL}'}}"
    )


def test_credential_alone_still_dispatches_the_credential():
    """Sans datastream, rien ne sait mieux : le provider reste le nom du module.

    C'est le chemin par connexion (herite) et les 37 connecteurs Nango, dont le
    credential EST le module. La reparation ne doit pas leur retirer leur nom.
    """
    rec = _Recorder()
    job = _job()
    job["datastream_id"] = None

    from core import quota

    with (
        patch("core.db.get_connection", new=_fake_get_connection),
        patch("core.main.get_module_pull_fn", return_value=_pull_fn),
        patch("core.queue._get_manifest_for_module", side_effect=rec.manifest_for),
        patch("core.queue._resolve_catalog_selection", side_effect=rec.catalog_selection),
        patch.object(quota, "get_read_cost", side_effect=rec.get_read_cost),
        patch.object(quota, "pre_check", side_effect=rec.pre_check),
        patch.object(quota, "record_spend", side_effect=rec.record_spend),
        patch(
            "core.verification.run_post_pull_verification",
            side_effect=rec.run_verification,
        ),
        patch(
            "core.context_seed.seed_project_context_best_effort", side_effect=rec.seed
        ),
        patch("core.audit.write_audit_row", side_effect=rec.audit),
    ):
        from core.queue import _execute_job

        _execute_job(job)

    assert set(rec.quota_read_cost) == {CREDENTIAL}
    assert set(rec.manifest_names) == {CREDENTIAL}
    assert rec.seed_names == [[CREDENTIAL]]


def test_pull_fn_is_resolved_by_module():
    """La reparation deja en place (ligne 900) ne doit pas regresser."""
    rec = _Recorder()
    resolved: list[str] = []

    def _capture(name, profile_id=None):
        resolved.append(name)
        return _pull_fn

    from core import quota

    with (
        patch("core.db.get_connection", new=_fake_get_connection),
        patch("core.main.get_module_pull_fn", side_effect=_capture),
        patch("core.queue._get_manifest_for_module", side_effect=rec.manifest_for),
        patch("core.queue._resolve_catalog_selection", side_effect=rec.catalog_selection),
        patch.object(quota, "get_read_cost", side_effect=rec.get_read_cost),
        patch.object(quota, "pre_check", side_effect=rec.pre_check),
        patch.object(quota, "record_spend", side_effect=rec.record_spend),
        patch(
            "core.verification.run_post_pull_verification",
            side_effect=rec.run_verification,
        ),
        patch(
            "core.context_seed.seed_project_context_best_effort", side_effect=rec.seed
        ),
        patch("core.audit.write_audit_row", side_effect=rec.audit),
    ):
        from core.queue import _execute_job

        _execute_job(_job())

    assert resolved == [MODULE]


def test_module_is_resolved_once_not_per_consumer():
    """Une seule lecture de `app.datastreams.module_name` par job.

    Huit consommateurs corriges a huit resolutions independantes seraient huit
    requetes -- et huit occasions de diverger. Le nom se resout une fois.
    """
    rec = _Recorder()
    module_queries: list[str] = []

    real_cursor_execute = _RoutingCursor.execute

    def _counting_execute(self, sql, params=None):
        if "SELECT module_name FROM app.datastreams" in " ".join(str(sql).split()):
            module_queries.append(str(params))
        return real_cursor_execute(self, sql, params)

    with patch.object(_RoutingCursor, "execute", _counting_execute):
        _run(rec)

    assert len(module_queries) == 1, (
        f"module resolu {len(module_queries)} fois, attendu 1"
    )


def test_unknown_pull_function_names_the_module_in_the_error():
    """Le message d'erreur doit nommer ce qu'on a cherche, pas le credential."""
    rec = _Recorder()
    errors: list[str] = []

    def _audit(**kwargs):
        rec.audit(**kwargs)
        meta = kwargs.get("metadata") or {}
        if meta.get("error"):
            errors.append(meta["error"])

    from core import quota

    with (
        patch("core.db.get_connection", new=_fake_get_connection),
        patch("core.main.get_module_pull_fn", return_value=None),
        patch("core.queue._get_manifest_for_module", side_effect=rec.manifest_for),
        patch.object(quota, "get_read_cost", side_effect=rec.get_read_cost),
        patch.object(quota, "pre_check", side_effect=rec.pre_check),
        patch("core.audit.write_audit_row", side_effect=_audit),
    ):
        from core.queue import _execute_job

        _execute_job(_job())

    assert errors, "aucune ligne d'audit d'echec"
    assert MODULE in errors[0], f"l'erreur ne nomme pas le module cherche : {errors[0]}"


# ---------------------------------------------------------------------------
# 2026-08-30 -- UN CONSENTEMENT COUVRE N CONNECTEURS ET LA SELECTION N'EN
# DISTINGUAIT QU'UN (`data-path.md`, « Incomplete if » [4]).
#
# Le repli de `_resolve_selected_account` lisait
# `... WHERE connection_ref_id = %s AND state='ready' ORDER BY verified_at DESC
# LIMIT 1` : le compte le plus recemment verifie du consentement ENTIER, quel
# que soit l'outil qui tire. Un consentement Google en couvre sept, chacun avec
# son propre espace de comptes, et la 211 laisse
# `app.datastreams.source_account_id` nullable -- donc la branche est atteignable.
#
# Ce que ces trois cas epinglent : lie gagne, un seul candidat n'est pas un pari,
# deux candidats se REFUSENT par leur nom.
# ---------------------------------------------------------------------------


def _resolved_account(*, bound: str | None, ready: list[tuple[str, str | None]]):
    """Le compte que `_execute_job` porterait jusqu'au pull, sous ce consentement."""
    from core.queue import _resolve_selected_account

    with _consent(bound=bound, ready=ready), _fake_get_connection() as conn:
        return _resolve_selected_account(conn, "conn_ref_ai95", "ds_ai95", connector=MODULE)


def test_the_datastreams_own_binding_wins_over_the_consent():
    """Le choix de l'operateur est la reponse, et rien ne la reinterprete."""
    assert (
        _resolved_account(
            bound="sc-domain:example.com",
            ready=[
                ("sc-domain:other.example", MODULE),
                ("properties/222", "google-analytics"),
            ],
        )
        == "sc-domain:example.com"
    )


def test_one_ready_account_of_the_connector_is_not_a_guess():
    """Un seul compte lisible par CE connecteur : le prendre ne choisit rien.

    C'est le cas des Datastreams anterieurs a la 211 : ils tiraient deja celui-la.
    """
    assert (
        _resolved_account(
            bound=None,
            ready=[("sc-domain:example.com", MODULE), ("properties/222", "google-analytics")],
        )
        == "sc-domain:example.com"
    )


def test_a_pre_210_account_stays_admissible_for_any_connector():
    """`discovered_for_connector` NULL veut dire « inconnu », pas « d'un autre ».

    Migration 210 le dit dans son propre commentaire, et `account_connector_sql`
    l'applique partout ailleurs. Narrower ici priverait un compte decouvert avant
    la 210 du seul connecteur qui peut le lire.
    """
    assert _resolved_account(bound=None, ready=[("sc-domain:example.com", None)]) == (
        "sc-domain:example.com"
    )


def test_two_ready_accounts_of_the_connector_refuse_by_name():
    """Deux proprietes du meme outil : il n'y a pas de reponse, il y a un refus."""
    from core.queue import AccountSelectionAmbiguous

    with pytest.raises(AccountSelectionAmbiguous) as raised:
        _resolved_account(
            bound=None,
            ready=[("sc-domain:a.example", MODULE), ("sc-domain:b.example", MODULE)],
        )

    assert raised.value.connector == MODULE
    assert len(raised.value.accounts) == 2


def test_the_ambiguous_job_dead_letters_and_never_pulls():
    """AUCUN TIRAGE SOUS UN PARI : le job meurt, et le pull n'est pas appele.

    Le message nomme le geste qui repare -- lier le compte sur le Datastream --
    et ne cite ni table ni colonne.
    """
    from core import quota
    from core.queue import REFUSAL_ACCOUNT_SELECTION_AMBIGUOUS

    finished: list[tuple] = []
    pulled: list[str] = []

    def _watch_finish(conn, job, state, **kwargs):
        finished.append((state, kwargs.get("error_detail")))

    def _never(*args, **kwargs):
        pulled.append("pull")
        return {"pull_id": "pull_ai95_01", "row_count": 7}

    rec = _Recorder()
    with (
        _consent(
            bound=None,
            ready=[("sc-domain:a.example", MODULE), ("sc-domain:b.example", MODULE)],
        ),
        patch("core.db.get_connection", new=_fake_get_connection),
        patch("core.main.get_module_pull_fn", return_value=_never),
        patch("core.queue._get_manifest_for_module", side_effect=rec.manifest_for),
        patch("core.queue._finish_job", side_effect=_watch_finish),
        patch.object(quota, "get_read_cost", side_effect=rec.get_read_cost),
        patch.object(quota, "pre_check", side_effect=rec.pre_check),
        patch("core.audit.write_audit_row", side_effect=rec.audit),
    ):
        from core.queue import DEAD_LETTER, _execute_job

        assert _execute_job(_job()) is True

    assert pulled == [], "un pull a eu lieu alors que le compte n'etait pas connu"
    assert finished and finished[0][0] == DEAD_LETTER, finished
    message = finished[0][1] or ""
    assert "choose the account it reads" in message, message
    for term in ("connection_account_scope", "credential_accounts", "source_account_id"):
        assert term not in message, f"le refus cite la base : {message}"
    assert REFUSAL_ACCOUNT_SELECTION_AMBIGUOUS == "account_selection_ambiguous"
