"""Shared pytest fixtures for the server test suite.

Provides the ``live_postgres`` fixture (AI-37, Story 6.1, AC14): a real psycopg
connection to the test Postgres, used to verify schema constraints (NOT NULL / FK /
UNIQUE) that mocked cursors cannot catch. Skips when ``TEST_POSTGRES_DSN`` is unset.

── Pourquoi un garde-fou sur la DSN (2026-07-27) ────────────────────────────
Ces tests ne se contentent pas de lire : ils INSERENT et COMMITENT (il faut un
vrai commit pour prouver une contrainte). Tant que ``TEST_POSTGRES_DSN`` a pointé
sur la base de PRODUCTION, chaque exécution y a donc laissé ses lignes -- des
procédures et des topics au scope PLATEFORME, c'est-à-dire visibles depuis tous
les projets de tous les clients. Le mindmap d'un projet neuf affichait 15 noeuds
de fixtures.

Deux protections, dans cet ordre :

1. ``_refusal_reason`` refuse une base distante non déclarée. Une base jetable
   (locale, ou branche Supabase dédiée) ne demande rien ; une base distante
   exige ``TEST_POSTGRES_ALLOW_REMOTE=1``, donc un geste conscient. Le refus
   tombe sur les tests qui ALLAIENT écrire, pas sur la session entière.
2. ``_pg_session_scrub`` supprime, en fin de session, ce que la session a écrit
   hors de tout projet actual. Filet de sécurité, PAS une permission : il ne
   rattrape pas tout (les tables ``*_versions`` sont append-only et refusent le
   DELETE par trigger -- migration 031, et 099 les exclut délibérément de
   l'échappatoire RGPD). Ce qu'il ne peut pas nettoyer, il le DIT.
"""

from __future__ import annotations

import os
import pathlib
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ── AI-106 : l'ancre de racine de depot, UNE fois pour toute la suite ─────────
#
# Defaut mesure le 2026-08-01 : 25 tests passaient ou echouaient selon le
# repertoire d'invocation, parce qu'ils lisaient un fichier du depot par un
# chemin RELATIF AU CWD -- `Path("infra/nango/migrations/114_*.sql")`,
# `Path("server/core/operations_mcp.py")`, `Path("ui/admin/src/shell/…")` cote
# racine, et `Path("core/main.py")` cote `server/`. Les deux jeux d'echecs
# n'etaient pas emboites : 22 rouges seulement depuis `server/`, 3 rouges
# seulement depuis la racine.
#
#     cd server && uv run python -m pytest tests -q -n 8   -> 100 failed
#     uv run python -m pytest server/tests -q -n 8         ->  81 failed
#
# Tant que ce chiffre depend du CWD, aucun compte d'echecs n'est comparable a un
# autre. La reparation est de CLASSE (CLAUDE.md §4) : un seul point de verite,
# resolu depuis `__file__`, jamais depuis le CWD ni depuis une variable
# d'environnement. `server/tests/__init__.py` existe, donc pytest importe ce
# fichier sous le nom `tests.conftest` : les modules de test le consomment par
# `from tests.conftest import REPO_ROOT`, quel que soit le repertoire de lancement.
#
# NB -- ce qui n'est PAS un defaut de cette classe : le defaut de `TENANT_KEY_DIR`
# est un chemin relatif PAR CONCEPTION (`core/tenant_keys.py:107` ->
# `os.environ.get("TENANT_KEY_DIR", "infra/keys")` ; CLAUDE.md §6, « un script
# lance hors de la racine ne dechiffre aucun token »). Ne pas l'ancrer ici :
# c'est un choix, pas un oubli. Et il ne fait diverger AUCUN test -- mesure du
# 2026-08-01, `grep -rn TENANT_KEY_DIR server/tests --include="*.py"` -> 13 sites
# dans 6 fichiers, tous en `setenv`/`patch.dict` vers un `tmp_path` ABSOLU. Aucun
# test ne s'appuie sur le defaut relatif, donc aucun ne change de verdict avec le
# repertoire d'invocation.

#: Racine du depot : <repo>/server/tests/conftest.py -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Racine du paquet serveur, pour les chemins ecrits relativement a `server/`.
SERVER_ROOT = REPO_ROOT / "server"

# ── AI-74 : pourquoi la suite « ne terminait pas en 40 minutes » ──────────────
#
# Ce n'etait PAS le volume. Mesure du 2026-07-31, sans TEST_POSTGRES_DSN :
#
#     cd server && python -m pytest tests/core -q
#     -> 7285 tests, 337 s (5 min 37)
#
# Le blocage vient d'une seule chose : libpq attend la duree du timeout TCP du
# systeme -- MESURE a 130,1 s -- quand la DSN pointe un hote qui ne repond pas
# (base locale eteinte, pooler Supabase en pause, VPN). Et il y a 135 appels a
# `psycopg.connect()` dans tests/core, dont aucun ne passait `connect_timeout`.
# Reproduit :
#
#     TEST_POSTGRES_DSN=postgresql://u:p@10.255.255.1:5432/connector_test \
#       python -m pytest tests/core/test_epic38_connector_domain.py -q
#     -> UN fichier, pas fini en 600 s
#     ... la meme commande avec PGCONNECT_TIMEOUT=5 : 51,7 s, 6 echecs francs
#
# `PGCONNECT_TIMEOUT` est lu par libpq pour TOUTE connexion qui ne fixe pas
# `connect_timeout` elle-meme : le poser ici traite les 135 appels d'un coup,
# sans toucher un seul fichier de test (CLAUDE.md §4 -- la classe, pas
# l'exemplaire). `setdefault` : une valeur posee sciemment dans le shell gagne.
os.environ.setdefault("PGCONNECT_TIMEOUT", "5")

#: Timeout de connexion effectif, relu pour les sondes de ce fichier.
_PG_CONNECT_TIMEOUT = int(os.environ.get("PGCONNECT_TIMEOUT") or "5")

# ── Les trois moteurs de fond, éteints AVANT tout import de module de test ────
#
# 162 fichiers de test faisaient `os.environ.setdefault("HEALTH_POLLER_ENABLED",
# "false")` à l'import, et 15 fichiers construisent `build_asgi_app()` SANS le
# faire. `setdefault` veut dire que le premier import gagne : il suffisait qu'un
# de ces 15 soit importé en premier pour que le poller, le worker de queue et le
# scheduler démarrent -- pour toute la session, pas pour un test.
#
# Et comme ces moteurs tournent sur MINUTERIE, leur interférence ne dépend pas de
# l'ordre (déterministe ici, aucun plugin `randomly` n'est installé) mais du
# MOMENT. C'est la forme exacte du défaut observé le 2026-07-31 : deux passes
# identiques sur base vierge, 156 échecs puis 96, la différence entière portée
# par les suites `*_seams.py` qui passent 26/26 en isolation.
#
# conftest.py est importé avant tout module de test, donc poser les valeurs ici
# gagne contre chaque `setdefault`. C'est un `=`, pas un `setdefault` : une
# valeur héritée du shell ne doit pas pouvoir rallumer un moteur de fond au
# milieu d'une suite.
for _background_engine in (
    "HEALTH_POLLER_ENABLED",
    "QUEUE_WORKER_ENABLED",
    "SCHEDULER_ENABLED",
    "MANAGED_FEED_SYNC_ENABLED",
    # AI-346: the startup recompilation sweep opens its own connection and
    # COMMITS -- a fourth engine on a timer of one, and the same interference.
    "SEMANTIC_RECOMPILE_ON_START",
):
    os.environ[_background_engine] = "false"

#: Organisation de fixture, partagée par toute la suite.
#:
#: Tout projet appartient à une organisation, SANS EXCEPTION : c'est la chaîne du
#: produit (org -> projet -> datastream, et le credential au niveau org). Les
#: tests qui insèrent un projet directement doivent donc fournir une org, comme
#: n'importe quel appelant. Cette constante leur en donne une, créée une fois par
#: session, plutôt que d'obliger chaque fichier à en fabriquer une.
TEST_ORG_ID = "org_test_fixture"

#: Hôtes considérés comme jetables : une base qu'on peut repeupler sans conséquence.
_DISPOSABLE_HOSTS = frozenset(
    {"", "localhost", "127.0.0.1", "::1", "host.docker.internal", "db", "postgres"}
)

#: Opt-in explicite pour une base distante (branche de test Supabase, CI dédiée).
_ALLOW_REMOTE_ENV = "TEST_POSTGRES_ALLOW_REMOTE"

#: created_by des lignes écrites par les fixtures. Sert au balayage de fin de
#: session pour distinguer un déchet de test d'une donnée plateforme légitime.
_TEST_AUTHORS = ("test", "t", "tests", "system:test")

#: L'auteur de l'org de fixture, et DÉLIBÉRÉMENT hors de `_TEST_AUTHORS`.
#:
#: Deux exigences se contredisaient et cette constante les tient toutes les deux.
#: L'org de fixture ne doit PAS être balayée en fin de session (voir `test_org` :
#: d'autres sessions s'y rattachent, la détruire ferait tomber leurs projets),
#: donc son auteur ne peut pas figurer dans `_TEST_AUTHORS`. Mais il doit se
#: reconnaître au premier coup d'œil dans une base.
#:
#: Il valait `system` jusqu'au 2026-08-04, et ça a coûté une fausse alerte de
#: sécurité (AI-183) : une ligne `app.org_members` avec `identity='system'`,
#: `role='owner'`, `status='active'` en PRODUCTION se lit comme une identité
#: non-humaine propriétaire d'une organisation cliente. C'en était une -- mais
#: c'était cette fixture, arrivée là avant que `_refusal_reason` n'existe. Un
#: nom qui dit ce qu'il est aurait répondu à la question sans investigation.
FIXTURE_AUTHOR = "test-fixture"


def _dsn_parts(dsn: str) -> tuple[str, str]:
    """(host, dbname) d'une DSN, quel que soit son format. ('', '') si illisible."""
    try:
        import psycopg.conninfo  # noqa: PLC0415

        info = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:
        return ("", "")
    return (str(info.get("host") or ""), str(info.get("dbname") or ""))


def _looks_disposable(host: str, dbname: str) -> bool:
    """Vrai si la base peut être polluée sans conséquence."""
    if host.lower() in _DISPOSABLE_HOSTS:
        return True
    # Une base distante DÉDIÉE aux tests se nomme comme telle. Un simple
    # "postgres" (le défaut Supabase) ne passe pas.
    lowered = dbname.lower()
    return lowered.endswith("_test") or lowered.startswith("test_") or lowered == "test"


def _refusal_reason(dsn: str) -> str | None:
    """Pourquoi cette DSN est refusée, ou None si elle est utilisable."""
    if os.environ.get(_ALLOW_REMOTE_ENV) == "1":
        return None

    host, dbname = _dsn_parts(dsn)
    if _looks_disposable(host, dbname):
        return None

    return (
        f"TEST_POSTGRES_DSN pointe sur une base distante non declaree jetable "
        f"(host={host or '?'}, dbname={dbname or '?'}).\n"
        "Ces tests COMMITENT des lignes, dont certaines au scope PLATEFORME "
        "(project_id NULL), visibles depuis TOUS les projets. Pointer la "
        "production ici, c'est y injecter des fixtures a chaque execution.\n\n"
        "Choisir l'un des deux :\n"
        "  - une base jetable : Postgres local, ou une branche Supabase dediee "
        "(nom de base se terminant par _test) ;\n"
        f"  - si c'est deliberement une base distante et jetable : {_ALLOW_REMOTE_ENV}=1."
    )


@pytest.fixture(scope="session")
def _test_dsn():
    """La DSN si elle est utilisable, None sinon.

    Le refus ne fait PAS tomber la session : les milliers de tests qui ne
    touchent pas Postgres doivent rester exécutables avec une DSN de production
    dans l'environnement du shell. Il tombe là où on écrirait -- dans
    ``live_postgres`` (échec franc, jamais un skip : un skip laisserait croire
    que le gate pg a tourné) -- et il empêche toute écriture ailleurs.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        return None
    return None if _refusal_reason(dsn) else dsn


@pytest.fixture(scope="session", autouse=True)
def test_org(_test_dsn):
    """Garantit l'existence de TEST_ORG_ID quand un Postgres actual est branché.

    Idempotente et JAMAIS supprimée en fin de session : plusieurs fichiers de
    test (voire plusieurs sessions en parallèle) s'y rattachent, et la détruire
    ferait tomber les projets des autres. C'est une donnée de socle, pas un
    artefact de test.
    """
    dsn = _test_dsn
    if not dsn:
        return TEST_ORG_ID

    import psycopg  # noqa: PLC0415

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                # D'abord renommer l'ancienne fixture, ENSUITE insérer. Les
                # deux INSERT sont `ON CONFLICT DO NOTHING` : sur une base qui
                # porte encore la ligne `system`, insérer d'abord ajouterait un
                # SECOND membre owner au lieu de corriger le premier. Scopé aux
                # deux ids de la fixture, et idempotent.
                cur.execute(
                    "UPDATE app.organizations SET created_by = %s "
                    "WHERE id = %s AND created_by = 'system'",
                    (FIXTURE_AUTHOR, TEST_ORG_ID),
                )
                cur.execute(
                    "UPDATE app.org_members SET identity = %s "
                    "WHERE id = 'omem_test_fixture' AND org_id = %s AND identity = 'system'",
                    (FIXTURE_AUTHOR, TEST_ORG_ID),
                )
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Test fixture org', 'org-test-fixture', 'active', %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (TEST_ORG_ID, FIXTURE_AUTHOR),
                )
                # Un membre, sinon l'org est SANS MEMBRE -- donc default-open
                # jusqu'a enrolement (21.5) et visible par n'importe quelle
                # identite. Une org de fixture ne doit pas apparaitre dans le
                # cloisonnement mesure par le harnais.
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES ('omem_test_fixture', %s, %s, 'owner', 'active', NOW()) "
                    "ON CONFLICT (org_id, identity) DO NOTHING",
                    (TEST_ORG_ID, FIXTURE_AUTHOR),
                )
            conn.commit()
    except Exception:
        # Pas de Postgres joignable : les tests pg-gated se sauteront d'eux-mêmes.
        pass
    return TEST_ORG_ID


# ---------------------------------------------------------------------------
# Démontage d'une org de fixture
# ---------------------------------------------------------------------------


def purge_fixture_org(conn, org_id: str) -> None:
    """Efface l'arbre d'une org de fixture, PUIS l'org -- par le chemin produit.

    Pourquoi ce helper existe (mesuré 2026-08-15). Un démontage de fixture qui
    écrit sa propre liste de ``DELETE`` se périme dès qu'une migration ajoute une
    table gouvernée qui référence ``app.projects`` ou ``app.organizations`` en
    ``ON DELETE RESTRICT`` et qu'un semeur la remplit toute seule. Deux exemples
    du même jour, sur UNE fixture : ``project_capabilities`` (migration 243) puis
    ``mdm_business_domains``. La liste écrite à la main perd la course, toujours.

    Ce que fait ce helper à la place : il appelle ``core.org_purge.purge_org_tree``
    -- le graphe de clés étrangères que la production parcourt déjà -- donc une
    table ajoutée demain est démontée sans que personne y pense. Le harnais et le
    produit ne peuvent pas diverger, puisqu'il n'y a qu'un seul plan.

    Le commit appartient à l'appelant, comme pour ``purge_org_tree``.
    """
    from core.org_purge import purge_org_tree  # noqa: PLC0415 -- import paresseux

    purge_org_tree(conn, org_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))


@pytest.fixture()
def production_auth_mode(monkeypatch):
    """`TOOROW_AUTH_MODE=oauth` -- sans quoi AUCUNE decision d'acces n'est rendue.

    `resolve_strict_resource_access` refuse par `production_identity_required` des
    que le mode vaut `disabled`, qui est le defaut de la suite. Une route qui
    prend une decision de production repond alors 404 ou 403 pour une raison
    ETRANGERE a ce que le test verifie -- mesure sur trois fichiers, dont l'un
    rendait sept routes cassees pour une seule variable d'environnement.

    C'est une fixture DECLAREE, jamais autouse : plusieurs tests mesurent
    justement l'echappatoire de l'operateur local en mode `disabled`, et la leur
    retirer d'office remplacerait un faux rouge par un faux vert.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    # Des que l'authentification n'est plus `disabled`, le produit EXIGE ce
    # secret (`analyze_feedback.py:91-93`) et refuse au demarrage sans lui -- il
    # signe le contexte de feedback, et un contexte non signe est un contexte
    # qu'on ne peut pas croire. Une fixture qui allume l'auth sans lui deplace
    # simplement le refus.
    monkeypatch.setenv(
        "TOOROW_FEEDBACK_CONTEXT_SECRET", "test-fixture-feedback-secret-not-a-real-one"
    )
    yield


def canonical_person(cur, subject: str) -> str:
    """Le `person_<ULID>` derriere un sujet, enregistre s'il est neuf.

    L'APPARTENANCE NE CONNAIT QUE L'IDENTITE CANONIQUE. `app.org_members.identity`
    et `app.resource_grants.identity` portent `person_<ULID>` ; un sujet comme une
    adresse e-mail est resolu par `app.person_identities`
    (`core.identity_bridge`). Une fixture qui inscrit le sujet brut n'inscrit
    PERSONNE, et la mesure le dit sans ambiguite :
    `resolve_strict_resource_access` rend `production_identity_required` et la
    route repond 404 ou 403 -- pour une raison etrangere a ce que le test verifie.

    Ce n'est pas un raccourci autour du produit : c'est ce que le chemin d'entree
    ecrit, et les routes sous test le relisent exactement comme en production.
    """
    import uuid as _uuid

    cur.execute(
        "SELECT person_id FROM app.person_identities WHERE subject = %s LIMIT 1",
        (subject,),
    )
    row = cur.fetchone()
    if row and row[0]:
        return str(row[0])
    person = f"person_{_uuid.uuid4().hex[:26].upper()}"
    cur.execute("INSERT INTO app.persons (id) VALUES (%s)", (person,))
    # `verified_email` et `verified_email_at` sont NULL ensemble ou poses ensemble
    # (`person_identities_check`) : une verification sans moment n'en est pas une.
    cur.execute(
        "INSERT INTO app.person_identities (id, person_id, issuer, subject) "
        "VALUES (%s, %s, 'test', %s)",
        (f"pid_{_uuid.uuid4().hex[:12]}", person, subject),
    )
    return person


def enrol_fixture_identity(
    cur,
    subject: str,
    *,
    org_id: str = TEST_ORG_ID,
    project_id: str | None = None,
    org_role: str = "owner",
    capability: str = "manage",
) -> str:
    """Inscrit *subject* dans l'org, et sur un projet quand il y en a un.

    Les deux lignes que la migration 132 a mises a la place d'`app.project_members` :
    l'appartenance a l'ORG et une concession SCOPEE. `core.project_access` lit
    exactement ces deux-la, donc une fixture qui les ecrit inscrit comme le
    produit inscrit.

    Rend l'identite CANONIQUE, celle que les routes verront -- un appelant qui
    garde le sujet brut se refuse lui-meme l'acces qu'il vient d'accorder.
    """
    import uuid as _uuid

    person = canonical_person(cur, subject)
    cur.execute(
        "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
        "VALUES (%s, %s, %s, %s, 'active', NOW()) "
        "ON CONFLICT (org_id, identity) DO NOTHING",
        (f"omem_{_uuid.uuid4().hex[:12]}", org_id, person, org_role),
    )
    if project_id is not None:
        cur.execute(
            "INSERT INTO app.resource_grants "
            "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
            "VALUES (%s, %s, %s, 'project', %s, %s, 'test') "
            "ON CONFLICT (org_id, identity, scope_type, scope_id) DO NOTHING",
            (f"rgrant_{_uuid.uuid4().hex[:12]}", org_id, person, project_id, capability),
        )
    return person


def purge_fixture_project(conn, project_id: str) -> None:
    """Supprime un projet de fixture, quoi qu'une migration ait ajouté depuis.

    LE PROBLÈME, mesuré le 2026-08-15 (AI-291) : 25 démontages écrivent leur
    propre liste de ``DELETE``, et une liste écrite à la main perd la course.
    Sur UNE seule fixture, deux tables du même jour retenaient le projet en
    ``ON DELETE RESTRICT`` -- ``project_capabilities`` (migration 243), semée
    pour tout projet, puis ``mdm_business_domains``.

    LE CHEMIN RAPIDE D'ABORD, ET C'EST DÉLIBÉRÉ. Le plan complet du graphe coûte
    1 794 requêtes et 2,2 s par appel (mesuré) : correct, et inutilisable dans un
    démontage joué des centaines de fois. Or dans le cas courant le projet part
    en UNE requête -- tout le reste pend par ``ON DELETE CASCADE``, que Postgres
    fait lui-même.

    Le graphe ne sert donc que quand la liste a vieilli : une arête bloquante
    refuse le ``DELETE``, et c'est exactement le signal qu'une table gouvernée
    est apparue. Le prix est payé le jour où il achète quelque chose.

    Le ``SAVEPOINT`` est ce qui rend le repli possible : une transaction Postgres
    est abortée par l'échec, donc sans point de reprise la seconde tentative
    échouerait sur ``InFailedSqlTransaction`` plutôt que sur le vrai obstacle.
    """
    import psycopg

    with conn.cursor() as cur:
        # L'échappatoire est posée sur LES DEUX chemins, pas seulement sur le
        # repli : les enfants qui partent en CASCADE portent des triggers
        # d'immuabilité (`plan_allocation_daily`, `media_plan_versions`...) qui
        # refusent le DELETE sans elle. Sans ce SET LOCAL, le chemin rapide levait
        # une exception de trigger -- pas une `ForeignKeyViolation` -- donc le
        # repli ne l'attrapait même pas, et les fixtures désactivaient les
        # triggers à la main, ce qui exige d'être PROPRIÉTAIRE de la table.
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute("SAVEPOINT fixture_project_purge")
        try:
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        except psycopg.errors.ForeignKeyViolation:
            cur.execute("ROLLBACK TO SAVEPOINT fixture_project_purge")
        else:
            cur.execute("RELEASE SAVEPOINT fixture_project_purge")
            return

    # La liste a vieilli : le graphe, celui que la PRODUCTION parcourt.
    from core.org_purge import plan_purge  # noqa: PLC0415 -- import paresseux

    plan = plan_purge(
        conn, project_id, root_table="app.projects", root_predicate="id = %s"
    )
    with conn.cursor() as cur:
        # Même échappatoire que `purge_org_tree`, même raison : les tables
        # append-only refusent le DELETE sans elle (migration 098), et
        # `SET LOCAL` la borne à la transaction de l'appelant.
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        for op in plan:
            cur.execute(op.sql, (project_id,))
        cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))


# ---------------------------------------------------------------------------
# Balayage de fin de session
# ---------------------------------------------------------------------------


def _scrubbable_tables(conn) -> list[str]:
    """Tables app.* portant à la fois project_id et created_at.

    Découvertes dans le catalogue, pas listées à la main : une table ajoutée
    demain est balayée sans que personne ait à y penser.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema
             AND t.table_name = c.table_name
             AND t.table_type = 'BASE TABLE'
            WHERE c.table_schema = 'app' AND c.column_name = 'project_id'
              AND EXISTS (
                  SELECT 1 FROM information_schema.columns c2
                  WHERE c2.table_schema = 'app' AND c2.table_name = c.table_name
                    AND c2.column_name = 'created_at'
              )
            ORDER BY c.table_name
            """
        )
        return [r[0] for r in cur.fetchall()]


def _scrub_table(conn, table: str, since: datetime) -> tuple[int, str | None]:
    """Supprime les lignes écrites depuis `since` HORS de tout projet actual.

    Deux familles, et rien d'autre :
      - project_id renseigné mais absent de app.projects (projet fantôme) ;
      - project_id NULL (scope plateforme) ET created_by de fixture.

    Une ligne rattachée à un vrai projet n'est JAMAIS touchée : un test qui
    écrit dans un projet actual est un autre problème, que supprimer masquerait.
    """
    has_created_by = False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='app' "
            "AND table_name=%s AND column_name='created_by'",
            (table,),
        )
        has_created_by = cur.fetchone() is not None

    platform_clause = (
        " OR (t.project_id IS NULL AND t.created_by = ANY(%(authors)s))" if has_created_by else ""
    )
    sql = f"""
        DELETE FROM app.{table} t
        WHERE t.created_at >= %(since)s
          AND (
                (t.project_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM app.projects p WHERE p.id = t.project_id::text))
                {platform_clause}
          )
    """
    params: dict[str, object] = {"since": since, "authors": list(_TEST_AUTHORS)}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            deleted = cur.rowcount
        conn.commit()
        return (max(deleted, 0), None)
    except Exception as exc:
        conn.rollback()
        # Tables append-only (context_topics_versions, procedures_versions,
        # schema_context_versions -- trigger de la migration 031, et 099 les
        # exclut sciemment de l'echappatoire RGPD) : le DELETE est refuse par
        # conception. On le SIGNALE, on ne le contourne pas.
        return (0, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}")


@pytest.fixture(scope="session", autouse=True)
def _pg_session_scrub(_test_dsn):
    """Filet de sécurité : efface, en fin de session, ce que la session a semé.

    Ne remplace pas un teardown propre dans chaque test -- il rattrape ce qui
    passe entre les mailles, et surtout il DIT ce qu'il n'a pas pu nettoyer.
    """
    dsn = _test_dsn
    if not dsn:
        yield
        return

    started_at = datetime.now(timezone.utc)
    yield

    import psycopg  # noqa: PLC0415

    try:
        conn = psycopg.connect(dsn, connect_timeout=10)
    except Exception:
        return  # Pas de Postgres : les tests pg-gated se sont skippés.

    removed: dict[str, int] = {}
    refused: dict[str, str] = {}
    try:
        for table in _scrubbable_tables(conn):
            count, error = _scrub_table(conn, table, started_at)
            if count:
                removed[table] = count
            if error:
                refused[table] = error
    finally:
        conn.close()

    if removed:
        total = sum(removed.values())
        detail = ", ".join(f"{t}={n}" for t, n in sorted(removed.items()))
        warnings.warn(
            f"pg scrub: {total} ligne(s) de fixture supprimee(s) hors projet reel ({detail})",
            stacklevel=1,
        )
    if refused:
        detail = "; ".join(f"{t}: {e}" for t, e in sorted(refused.items()))
        warnings.warn(
            "pg scrub: nettoyage REFUSE sur "
            f"{len(refused)} table(s) -- ces lignes restent en base ({detail})",
            stacklevel=1,
        )


def _connected_role_is_privileged(conn) -> tuple[str, bool]:
    """Return the connected role and whether it owns/bypasses the app schema.

    "Privileged" here means exactly what decides the two test families: a
    superuser, or the owner of the app tables. Both bypass RLS -- the owner
    unless every policy is FORCEd, the superuser always.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
        role, is_superuser = cur.fetchone()
        cur.execute(
            "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class "
            "WHERE oid = to_regclass('app.datastreams')"
        )
        row = cur.fetchone()
    owns_app_tables = bool(row and row[0])
    return str(role), bool(is_superuser) or owns_app_tables


#: Everything the MCP capability registry has ever held. See the fixture below.
_MCP_REGISTRY_HIGH_WATER: dict[str, dict] = {
    "declarations": {},
    "app_only": set(),
    "widget_bound": {},
}


@pytest.fixture(autouse=True)
def _restore_mcp_capability_registry():
    """Put back what a test tore out of the PROCESS-GLOBAL capability registry.

    `core.mcp_profiles` keeps the catalog in three module-level containers, filled
    ONCE when `core.main` imports. `reset_registry_for_tests()` empties all three,
    and twelve test files call it -- almost always twice, before AND after their
    own work, so the catalog is left EMPTY for every test that runs afterwards.

    The damage is invisible until a later test reads the real catalog:

        pytest tests/core/test_epic36_capability_catalogs.py \
               tests/integration/test_mcp_data_render_split.py -p no:randomly
        -> test_app_only_tools_declare_app_visibility fails on frozenset()

    That red is one of the three listed in AI-130 as "unstable" and attributed to
    DuckDB artefacts shared between xdist workers. For this one the cause is here:
    no DuckDB, no xdist, and it reproduces deterministically on two files. A
    per-worker DuckDB path would not have fixed it.

    Repaired at the harness rather than at the ~40 call sites, because the
    registry is process-global and restoring it is the harness's job. This MERGES
    the high-water mark back instead of replacing: a test that legitimately
    registers something new keeps it, and a test that cleared the catalog stops
    charging the next one for it.
    """
    yield

    try:
        from core import mcp_profiles  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- a suite that never touches MCP owes nothing
        return

    high_water = _MCP_REGISTRY_HIGH_WATER
    high_water["declarations"].update(mcp_profiles._REGISTRY.declarations)
    high_water["app_only"].update(mcp_profiles._APP_ONLY_TOOLS)
    high_water["widget_bound"].update(mcp_profiles._WIDGET_BOUND_TOOLS)

    mcp_profiles._REGISTRY.declarations.update(high_water["declarations"])
    mcp_profiles._APP_ONLY_TOOLS.update(high_water["app_only"])
    mcp_profiles._WIDGET_BOUND_TOOLS.update(high_water["widget_bound"])


@pytest.fixture(autouse=True)
def _isolate_candidate_landing_registry():
    """Keep execution-scoped landing evidence from leaking between tests.

    The production registry deliberately outlives ``candidate_execution()`` so
    the dispatch driver can read the observed relation after the connector pull
    returns. A pytest worker, however, reuses one process for unrelated tests;
    fixture execution ids such as ``dse_test_001`` are therefore not globally
    unique. Clear only at test boundaries, never inside the product lifecycle.
    """
    try:
        from core import raw_landing  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- suites that never import it owe nothing
        yield
        return

    raw_landing._LANDED_RELATIONS.clear()
    raw_landing._LANDED_COLUMNS.clear()
    try:
        yield
    finally:
        raw_landing._LANDED_RELATIONS.clear()
        raw_landing._LANDED_COLUMNS.clear()


@pytest.fixture(autouse=True)
def _enforce_pg_role_marker(request):
    """Honour ``pg_owner`` / ``pg_app_role`` however the test gets its connection.

    Deliberately autouse and independent of :func:`live_postgres`. Several files
    open their own ``psycopg.connect(os.environ["TEST_POSTGRES_DSN"])`` in a
    private fixture instead of taking the shared one, so an enforcement that
    lived only in `live_postgres` would silently not apply to them -- and those
    are exactly the files whose DDL fixtures need the owner.

    A marker declares what the test needs; where it gets its connection is an
    implementation detail that must not decide whether the declaration is read.

    AND IT NO LONGER ONLY SAYS NO (2026-08-17). The skip below used to instruct
    the reader to "point TEST_POSTGRES_DSN at the schema owner for this file" --
    an instruction the harness can carry out itself, since
    `scripts/disposable_postgres.py env` exports ``TEST_POSTGRES_OWNER_DSN``.
    For a `pg_owner` test the variable is repointed FOR THE DURATION OF THAT TEST,
    so a file opening its own `psycopg.connect(os.environ["TEST_POSTGRES_DSN"])`
    gets the owner without knowing anything about roles. Measured before the
    change: 84 `InsufficientPrivilege` errors across `tests/integration`, and
    every `pg_owner` test skipped rather than run.
    """
    if not (
        request.node.get_closest_marker("pg_owner")
        or request.node.get_closest_marker("pg_app_role")
    ):
        return
    raw = os.environ.get("TEST_POSTGRES_DSN")
    if not raw or _refusal_reason(raw):
        # The test's own skip (or `live_postgres`'s hard failure on a refused
        # DSN) will fire. Do not open a connection just to say so.
        return

    owner = _owner_dsn_for(request)
    if owner and owner != raw:
        #  LES DEUX VARIABLES, parce que les tests atteignent la base par deux
        #  chemins : leur propre `psycopg.connect(os.environ["TEST_POSTGRES_DSN"])`
        #  et le `core.db.get_connection` du PRODUIT, qui lit `PLATFORM_DB_URL`.
        #  N en repointer qu une laissait la moitie des tests marques mourir sur
        #  << doit etre le proprietaire de la table ... >> apres avoir cesse de se
        #  sauter -- un rouge deplace, pas un rouge repare.
        #
        #  Repointe, jamais elargi : `pg_app_role` n arrive jamais ici, parce
        #  qu un test qui affirme la RLS et tournerait en proprietaire passerait
        #  en ne prouvant RIEN -- ce que `_enforce_declared_role` refuse par son
        #  nom, quelques lignes plus bas.
        patch = request.getfixturevalue("monkeypatch")
        patch.setenv("TEST_POSTGRES_DSN", owner)
        patch.setenv("PLATFORM_DB_URL", owner)
        raw = owner

    import psycopg  # noqa: PLC0415

    with psycopg.connect(raw, connect_timeout=_PG_CONNECT_TIMEOUT) as conn:
        _enforce_declared_role(conn, request)


def _owner_dsn_for(request) -> str | None:
    """The owning DSN for a ``pg_owner`` test, or ``None`` when there is none.

    THE DEFECT THIS CLOSES, measured 2026-08-17. `_enforce_declared_role` SKIPPED
    a `pg_owner` test whose connection was the application role, telling the
    reader to "point TEST_POSTGRES_DSN at the schema owner for this file" -- so
    running the suite once could never run both families, and the honest skip
    quietly became "this was never measured". Meanwhile
    `scripts/disposable_postgres.py env` has been exporting
    ``TEST_POSTGRES_OWNER_DSN`` all along, and five files already reached for it
    by hand.

    So the harness stops asking the person to re-point a DSN it already holds: a
    declared owner test is CONNECTED as the owner. The skip survives for the case
    it was written for -- no owning DSN exists at all -- because that is a real
    absence and not a preference.

    `pg_app_role` deliberately gets nothing here: it must run as the DEPLOYED
    application role, and handing it an owner would make it pass while proving
    nothing, which `_enforce_declared_role` refuses by name.
    """
    if not request.node.get_closest_marker("pg_owner"):
        return None
    owner = os.environ.get("TEST_POSTGRES_OWNER_DSN")
    if not owner or _refusal_reason(owner):
        return None
    return owner


def _enforce_declared_role(conn, request) -> None:
    """Skip, with the reason, when the connection cannot answer this test.

    The suite mixes two families that no single role satisfies, and running it
    under one role produced a large count of failures that measured the
    connection rather than the code (measured 2026-07-31: 88 failures as owner,
    109 as the application role, on the same tree).

      * ``pg_owner``    -- the test does DDL in its fixture (disable a trigger,
        ALTER TABLE). It needs ownership; as a plain role it dies on
        "must be owner of table ...".
      * ``pg_app_role`` -- the test asserts RLS. Run as owner or superuser it
        passes while proving NOTHING, which is worse than failing.

    A test with neither marker is unconstrained, so this changes nothing until a
    file opts in. A skip is the honest outcome: the test was not run, and saying
    so is not the same as saying it passed.
    """
    wants_owner = request.node.get_closest_marker("pg_owner")
    wants_app_role = request.node.get_closest_marker("pg_app_role")
    if not wants_owner and not wants_app_role:
        return

    role, privileged = _connected_role_is_privileged(conn)
    if wants_owner and not privileged:
        pytest.skip(
            f"needs an owning role for its DDL fixture; connected as {role!r}. "
            "Point TEST_POSTGRES_DSN at the schema owner for this file."
        )
    if wants_app_role and privileged:
        pytest.skip(
            f"asserts RLS and would pass vacuously as {role!r} (owner or superuser). "
            "Point TEST_POSTGRES_DSN at the deployed application role for this file."
        )


@pytest.fixture
def live_postgres(_test_dsn, request):
    """Yield a real psycopg connection to the test Postgres (AI-37).

    Skips the test when ``TEST_POSTGRES_DSN`` is not set — CI without a Postgres
    service, and contributors who have not opted in, run everything else. When set,
    the DSN points at a database where the app schema migrations (incl. 014) have
    been applied.

    A ``pg_owner`` test is CONNECTED AS THE OWNER when one is reachable — see
    :func:`_owner_dsn_for`. It only skips when no owning DSN exists; skipping
    while the harness exports one was the whole defect (2026-08-17).
    """
    raw = os.environ.get("TEST_POSTGRES_DSN")
    if not raw:
        pytest.skip("TEST_POSTGRES_DSN not set — skipping live Postgres integration test")
    refusal = _refusal_reason(raw)
    if refusal:
        # Echec, PAS skip : ce test allait ecrire dans cette base.
        pytest.fail(refusal, pytrace=False)
    dsn = _owner_dsn_for(request) or _test_dsn

    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(dsn, connect_timeout=_PG_CONNECT_TIMEOUT)
    try:
        _enforce_declared_role(conn, request)
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# AI-74 -- rendre la suite MESURABLE : couches, timeout, Postgres injoignable
#
# Rien ici n'annote un fichier de test. Tout est pose a la COLLECTE, par lecture
# du module deja importe : une suite de 7285 tests ne se marque pas a la main, et
# un marker recopie dans 398 fichiers derive des le lendemain.
#
# Mesures du 2026-07-31 (venv racine, 32 coeurs, TEST_POSTGRES_DSN absent) :
#
#   python -m pytest tests/core --collect-only -q   -> 7285 tests, 23,6 s
#   python -m pytest tests/core -q                  -> 337 s (5 min 37)
#   python -m pytest tests/core -q -n 8             -> 215 s (3 min 35)
#
# La collecte n'a jamais ete le probleme (23 s), ni le volume (5 min 37). Le seul
# blocage reel est celui documente en tete de ce fichier : PGCONNECT_TIMEOUT.
# ═══════════════════════════════════════════════════════════════════════════════

#: Modules et tests > 5 s, MESURES le 2026-07-31 par :
#:
#:     cd server && python -m pytest tests/core -q -n 8 \
#:         --durations=0 --durations-min=1.0
#:
#: Un prefixe qui se termine par `.py` marque tout le module ; un prefixe
#: `module.py::test_x` marque ce seul test. Ces quatre entrees pesent 205 s des
#: 337 s de la suite serie. Elles restent DANS le chemin par defaut : le marker
#: sert la boucle serree (`-m "not slow"`), il ne cache pas un cout.
_SLOW_PREFIXES = (
    # 64,0 s -- 24 tests, chacun reconstruit un cache DuckDB complet.
    "tests/core/test_warehouse_cache_routing.py",
    # 61,5 s pour CE seul test : il fabrique 15 000 elements factices.
    "tests/core/test_linkedin_ads_connector.py::test_near_cap_warning_logged",
    # 45,0 s -- 3 scenarios de rederivation FX de bout en bout.
    "tests/core/test_currency_rederivation.py",
    # 34,3 s -- 5 tests, chacun rejoue la porte d'audit sur l'arbre entier.
    "tests/core/test_finished_work_audit_data.py",
)

#: Markers deja poses a la main dans les fichiers et qui signifient tous
#: « il faut un vrai Postgres ». Les reconnaitre evite d'en inventer un sixieme.
_PG_MARKERS = frozenset({"pg_owner", "pg_app_role", "live_pg", "live_postgres", "isolation"})

#: Resultat des sondes de joignabilite : une par DSN distincte, une par session.
_PG_PROBE_CACHE: dict[str, str | None] = {}

#: Ce que la session n'a PAS pu executer, imprime en fin de session.
_PG_LAYER_NOTICES: list[str] = []

#: nodeid -> raison, pour les tests dont la DSN est REFUSEE (echec, pas skip).
_REFUSED_ITEMS: dict[str, str] = {}


def _pg_unreachable_reason(dsn: str) -> str | None:
    """None si le serveur repond, sinon la raison -- sonde UNE fois par DSN.

    Sans cette sonde, une base eteinte ne produit pas un skip mais une attente de
    ``PGCONNECT_TIMEOUT`` secondes REPETEE a chaque test qui ouvre sa propre
    connexion (135 sites dans tests/core). La sonde transforme N attentes en une.
    """
    if dsn in _PG_PROBE_CACHE:
        return _PG_PROBE_CACHE[dsn]

    try:
        import psycopg  # noqa: PLC0415

        psycopg.connect(dsn, connect_timeout=_PG_CONNECT_TIMEOUT).close()
        reason: str | None = None
    except Exception as exc:
        host, dbname = _dsn_parts(dsn)
        reason = (
            f"Postgres injoignable (host={host or '?'}, dbname={dbname or '?'}, "
            f"connect_timeout={_PG_CONNECT_TIMEOUT}s) : {type(exc).__name__}. "
            "La COUCHE pg N'A PAS TOURNE."
        )
    _PG_PROBE_CACHE[dsn] = reason
    return reason


def _item_dsn(item) -> str | None:
    """La DSN que CE test utiliserait, telle que son module la resout.

    Huit fichiers definissent ``_DSN = TEST_POSTGRES_DSN or PLATFORM_DB_URL`` :
    le repli sur ``PLATFORM_DB_URL`` court-circuite le garde-fou
    :func:`_refusal_reason`, donc une DSN de PRODUCTION presente dans le shell
    devient la base de test sans que rien ne le dise. On lit la valeur du module
    plutot que l'environnement, pour juger ce qui sera reellement utilise.
    """
    module_dsn = getattr(getattr(item, "module", None), "_DSN", None)
    if isinstance(module_dsn, str) and module_dsn:
        return module_dsn
    return os.environ.get("TEST_POSTGRES_DSN") or None


def _needs_pg(item) -> bool:
    """Vrai si CE test (pas son module) exige un vrai Postgres.

    Volontairement etroit. Une regle « le module mentionne TEST_POSTGRES_DSN »
    serait fausse : ``test_epic38_connector_domain.py`` porte 29 tests qui
    n'ouvrent aucune connexion et 6 qui en ouvrent une. Marquer le module entier
    ferait sauter les 29 des que la base est absente.
    """
    if any(item.get_closest_marker(name) for name in _PG_MARKERS):
        return True
    if "live_postgres" in getattr(item, "fixturenames", ()):
        return True
    # Convention du depot : 63 fonctions `test_live_*` ouvrent leur propre
    # connexion derriere un `@requires_postgres` maison.
    return item.name.startswith("test_live_")


#: chemin de module -> « ce module monte-t-il l'app ASGI ? », lu une seule fois.
_SEAM_MODULE_CACHE: dict[str, bool] = {}

#: Les deux signaux d'une couture HTTP. `hasattr(module, "TestClient")` seul ne
#: suffit PAS : sur les 52 fichiers de tests/core qui montent l'app, 21
#: seulement importent TestClient au niveau module ; les 31 autres l'importent
#: dans le corps du test, donc l'attribut n'existe pas. La lecture de source
#: rattrape ces 31 -- une fois par fichier, pas une fois par test.
_SEAM_TOKENS = ("TestClient", "build_asgi_app")


def _is_seam(item) -> bool:
    """Vrai si le module monte l'app ASGI (starlette TestClient / build_asgi_app)."""
    module = getattr(item, "module", None)
    if module is not None and any(hasattr(module, token) for token in _SEAM_TOKENS):
        return True

    path = getattr(item, "path", None)
    if path is None:
        return False
    key = str(path)
    cached = _SEAM_MODULE_CACHE.get(key)
    if cached is None:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        cached = any(token in source for token in _SEAM_TOKENS)
        _SEAM_MODULE_CACHE[key] = cached
    return cached


def _nodeid_matches(nodeid: str, prefixes: tuple[str, ...]) -> bool:
    """Le prefixe doit s'arreter sur une frontiere de nodeid, pas au milieu d'un nom.

    Sans cette condition, `test_cards.py` marquerait aussi `test_cards_api.py`
    des qu'un nom s'allonge -- une derive silencieuse, exactement ce qu'un
    registre mesure a la main doit eviter.
    """
    normalised = nodeid.replace("\\", "/")
    # Le nodeid peut etre prefixe du chemin d'invocation (`server/tests/...`
    # depuis la racine, `tests/...` depuis server/) : on compare sur le suffixe.
    return any(
        normalised == prefix
        or normalised.endswith("/" + prefix)
        or normalised.startswith(prefix + "::")
        or f"/{prefix}::" in normalised
        for prefix in prefixes
    )


def pytest_configure(config):
    """Declare les markers et verifie que le garde-fou anti-blocage est charge.

    Les markers sont declares ICI et pas seulement dans ``pyproject.toml`` parce
    que la suite a DEUX points d'entree qui ne lisent pas le meme fichier ini :
    ``make test`` (``uv run pytest server/tests`` depuis la racine, qui lit le
    ``pyproject.toml`` de la racine) et ``cd server && pytest tests`` (qui lit
    ``server/pyproject.toml``). Un marker declare d'un seul cote casse l'autre
    des que ``--strict-markers`` est actif.
    """
    for name, description in (
        ("unit", "ne monte pas l'app ASGI et ne touche pas Postgres"),
        ("seam", "monte l'app ASGI (starlette TestClient)"),
        ("pg", "exige un vrai Postgres (TEST_POSTGRES_DSN)"),
        ("slow", "> 5 s mesurees le 2026-07-31"),
    ):
        config.addinivalue_line("markers", f"{name}: {description}")

    if not config.pluginmanager.hasplugin("timeout"):
        warnings.warn(
            "pytest-timeout absent : un test qui pend BLOQUERA la session au lieu "
            "d'echouer (AI-74). Corriger par `uv sync` depuis la racine.",
            stacklevel=1,
        )
    elif getattr(config.option, "timeout", None) is None:
        # Point d'entree racine : le `pyproject.toml` de la racine ne porte pas
        # `timeout`. On le pose quand meme, sinon le garde-fou ne couvre que la
        # moitie des invocations.
        config.option.timeout = 180
        config.option.timeout_method = "thread"

    # La sonde est refaite ICI, et pas seulement a la collecte, parce que sous
    # xdist la collecte a lieu dans les WORKERS : `pytest_terminal_summary`
    # tourne dans le controleur, qui n'aurait donc rien a dire. Sans cela, la
    # commande de reference (parallele) tairait precisement ce qu'elle doit
    # crier -- qu'une couche entiere ne s'est pas executee.
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if dsn and not _refusal_reason(dsn):
        reason = _pg_unreachable_reason(dsn)
        if reason:
            _PG_LAYER_NOTICES.append(reason)
    elif dsn:
        host, dbname = _dsn_parts(dsn)
        _PG_LAYER_NOTICES.append(
            f"DSN REFUSEE (host={host or '?'}, dbname={dbname or '?'}) : base "
            "distante non declaree jetable. Les tests pg ECHOUENT sans se "
            "connecter -- ils n'ont rien prouve. Voir _refusal_reason()."
        )


#: Les operations qui EXIGENT d etre proprietaire d une table Postgres. Un
#: `CREATE TABLE` n en fait pas partie et c est deliberé : la plupart de ceux de
#: cette suite fabriquent une fixture DuckDB, et marquer dessus repointerait la
#: base applicative pour rien.
_OWNER_DDL = re.compile(
    r"(DISABLE\s+TRIGGER|ENABLE\s+TRIGGER|DROP\s+TRIGGER|session_replication_role"
    r"|ALTER\s+TABLE\s+app\.|TRUNCATE\s+(?:TABLE\s+)?app\.)",
    re.IGNORECASE,
)

#: UN TROISIEME SIGNAL A ETE ESSAYE PUIS RETIRE, et la raison vaut d etre lue.
#: Un fichier qui APPLIQUE une migration fait tout le DDL qu elle contient, donc
#: il a bien besoin du proprietaire. Le marquer le fait aboutir -- et c est
#: precisement le probleme : appliquer une migration ANCIENNE reinstalle son
#: corps de fonction pour TOUTE la session pytest.
#:
#: Mesure du 2026-08-17 : `sync_external_dispatch_excluded` est corrigee par les
#: migrations 103 puis 226 (`COALESCE(..., FALSE)`), toutes deux `applied`. Un
#: test qui rejoue `023_datastreams.sql` y remet le corps de la 076
#: (`NEW.source_kind = 'external_bq'`, qui rend NULL quand `source_kind` l est),
#: et les 113 `NotNullViolation` qui suivent frappent des fichiers qui n ont rien
#: demande. Reapplique a la main, la 226 : le corps etait redevenu casse au run
#: suivant.
#:
#: L echec de privilege EMPECHAIT ces tests d abimer le schema partage. Les
#: laisser aboutir sans isoler leur DDL echangerait 84 rouges de connexion contre
#: un schema qui derive sous les autres -- pire, parce que la victime n est plus
#: le test fautif. Isoler ce DDL est un chantier a part ; ce signal reste dehors
#: jusque-la.

#: Ce qui DISQUALIFIE un module, quoi qu il fasse d autre : il affirme que le
#: role connecte est ordinaire. Lui donner le proprietaire le ferait passer en ne
#: prouvant rien, ou echouer -- c est le tort exact que `pg_app_role` existe pour
#: empecher, et une derivation trop large l a produit une fois (2026-08-17) :
#: `test_render_shares_postgres` LIT la migration 248 pour affirmer son texte, et
#: son `test_the_connected_role_is_ordinary` a casse des qu il a eu le
#: proprietaire. Nommer un fichier de migration ne dit pas qu on l applique.
_CLAIMS_APP_ROLE = re.compile(r"pg_app_role|role_is_ordinary|is_ordinary", re.IGNORECASE)

#: `chemin du module -> fait-il du DDL de proprietaire`. Lu une fois par module.
_OWNER_DDL_CACHE: dict[str, bool] = {}


from tests.migration_ledger import (  # noqa: E402,F401 -- reexporte pour les fixtures
    apply_migrations_absent_from_the_ledger,
)


def _module_needs_owner(item) -> bool:
    """Le module de *item* fait-il du DDL qu un role applicatif ne peut pas faire ?

    DERIVE A LA COLLECTE, jamais recopie dans les fichiers. C est la doctrine que
    ce conftest applique deja au marker `slow` : *"une suite de 7285 tests ne se
    marque pas a la main, et un marker recopie dans 398 fichiers derive des le
    lendemain"*. Mesure du 2026-08-17 : 21 fichiers faisaient ce DDL sans le
    declarer, et leurs tests mouraient sur << doit etre le proprietaire de la
    table ... >> -- un rouge qui mesurait la CONNEXION et pas le code.

    Un fichier qui declare deja `pg_app_role` est laisse tranquille : il affirme
    la RLS, et lui donner le proprietaire le ferait passer en ne prouvant rien.
    """
    path = str(getattr(item, "fspath", "") or "")
    if not path:
        return False
    cached = _OWNER_DDL_CACHE.get(path)
    if cached is None:
        try:
            source = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        cached = bool(_OWNER_DDL.search(source)) and not _CLAIMS_APP_ROLE.search(source)
        _OWNER_DDL_CACHE[path] = cached
    return cached


def pytest_collection_modifyitems(config, items):
    """Pose les couches, et coupe court quand le Postgres declare ne repond pas."""
    unreachable: set[str] = set()
    refused: set[str] = set()

    for item in items:
        if _nodeid_matches(item.nodeid, _SLOW_PREFIXES):
            item.add_marker(pytest.mark.slow)

        needs_pg = _needs_pg(item)
        #  Le besoin de proprietaire se DECLARE ici, lu dans le module, pour que
        #  `_enforce_pg_role_marker` puisse repointer la base avant que le test
        #  ouvre sa connexion.
        #
        #  PAS conditionne a `needs_pg`, et c est mesure : `_needs_pg` reconnait
        #  la fixture partagee, et les fichiers qui ont exactement ce probleme
        #  sont ceux qui ouvrent leur PROPRE connexion sous un autre nom --
        #  `test_file_source_template_pg` en est un, et il restait rouge tant que
        #  la condition tenait. Un module qui ne touche jamais Postgres ne perd
        #  rien a porter le marker : sans DSN, rien ne se repointe.
        if _module_needs_owner(item):
            item.add_marker(pytest.mark.pg_owner)
        if _is_seam(item):
            item.add_marker(pytest.mark.seam)
        # `unit` est le seul marker exclusif : il affirme « ni ASGI ni Postgres ».
        item.add_marker(pytest.mark.pg if needs_pg else pytest.mark.unit)

        if not needs_pg:
            continue

        dsn = _item_dsn(item)
        if not dsn:
            continue  # le skip du fichier lui-meme dira « pas de DSN ».

        refusal = _refusal_reason(dsn)
        if refusal:
            # ECHEC, jamais skip -- meme convention que `live_postgres` : ce test
            # allait ECRIRE dans cette base. Un skip laisserait croire que rien
            # n'a ete tente.
            _REFUSED_ITEMS[item.nodeid] = refusal
            refused.add(dsn)
            continue

        reason = _pg_unreachable_reason(dsn)
        if reason:
            unreachable.add(dsn)
            item.add_marker(pytest.mark.skip(reason=reason))

    for dsn in sorted(unreachable):
        _PG_LAYER_NOTICES.append(_PG_PROBE_CACHE[dsn] or "")
    for dsn in sorted(refused):
        host, dbname = _dsn_parts(dsn)
        _PG_LAYER_NOTICES.append(
            f"DSN REFUSEE (host={host or '?'}, dbname={dbname or '?'}) : base "
            "distante non declaree jetable. Les tests pg ECHOUENT sans se "
            "connecter -- ils n'ont rien prouve. Voir _refusal_reason()."
        )


def pytest_runtest_setup(item):
    """Fait echouer, sans se connecter, un test dont la DSN est refusee."""
    refusal = _REFUSED_ITEMS.get(item.nodeid)
    if refusal:
        pytest.fail(refusal, pytrace=False)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Dire, en toutes lettres, ce que la session n'a pas execute.

    Un « vert » qui tait qu'une couche entiere ne s'est pas executee est un
    mensonge par omission -- c'est exactement ce que AI-74 corrige.
    """
    if not _PG_LAYER_NOTICES:
        return
    terminalreporter.write_sep("=", "COUCHE pg NON EXECUTEE", red=True)
    for notice in dict.fromkeys(_PG_LAYER_NOTICES):
        terminalreporter.write_line(f"  {notice}")
    terminalreporter.write_line(
        "  -> ce resultat ne dit RIEN des contraintes, des triggers ni de la RLS."
    )


# ---------------------------------------------------------------------------
# AI-130 -- un fichier DuckDB ne se partage pas entre workers
# ---------------------------------------------------------------------------
#
# Constat du 2026-08-01, en tracant l'ensemble instable de la suite (30/26/26
# echecs sur trois executions du MEME arbre). Les tests qui lisent le seed de
# developpement pointaient `TOOROW_DUCKDB_PATH` sur le fichier PARTAGE
# `server/modules/<module>/seeds/local.duckdb`. DuckDB prend un verrou EXCLUSIF
# sur le fichier : sous `-n 8`, deux workers qui l'ouvrent en meme temps se
# marchent dessus et l'un des deux echoue en « Device or resource busy ». Le
# test est correct, la donnee est correcte, et il rougit une fois sur trois.
#
# CE N'EST PAS UN CAS POUR UN RERUN-ON-FAILURE : masquer un defaut d'isolation
# le transforme en vert, et c'est exactement ce que ce depot retire partout
# ailleurs. La reparation est de donner a chaque worker SA copie -- lecture
# seule, donc une copie est fidele et rien ne se perd.
#
# Piege a ne pas re-decouvrir : `.duckdb` s'accompagne d'un `.wal` quand la base
# a ete fermee sans checkpoint. Copier le `.duckdb` seul rend une base qui
# s'ouvre et a laquelle il MANQUE les dernieres ecritures -- un faux vert. Les
# deux sont copies quand le second existe.


@pytest.fixture(scope="session")
def duckdb_seed_copy(tmp_path_factory, worker_id="master"):
    """Rend une fonction `(chemin_du_seed) -> copie privee a ce worker`.

    Skip explicite -- jamais un vert silencieux -- quand le seed est absent :
    la CI tourne sans les seeds et doit le DIRE plutot que de passer a vide.
    """
    import shutil

    base = tmp_path_factory.mktemp("duckdb-seeds")

    def _copy(seed_path: str | Path) -> str:
        src = Path(seed_path)
        if not src.exists():
            pytest.skip(f"seed DuckDB absent : {src}")
        dst = base / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            wal = src.with_suffix(src.suffix + ".wal")
            if wal.exists():
                shutil.copy2(wal, dst.with_suffix(dst.suffix + ".wal"))
        return str(dst)

    return _copy


# ---------------------------------------------------------------------------
# Un module ne choisit pas l'environnement du module suivant (AI-291)
# ---------------------------------------------------------------------------
#
# La MESURE et la règle vivent dans `tests/env_leak_guard.py`, pas ici : ce
# fichier est partagé par toutes les sessions et n'a besoin que du branchement.


@pytest.fixture(autouse=True, scope="module")
def _environment_survives_the_module(request):
    """Le module rend l'environnement qu'il a reçu, ou il est NOMMÉ.

    Portée MODULE et non fonction : le coût est de deux lectures de
    `os.environ` par fichier, et c'est la granularité du défaut -- une fuite
    intra-module est rattrapée par le teardown du module, une fuite inter-modules
    ne l'est par personne. Le rouge tombe au démontage du fichier fautif, jamais
    sur la victime.
    """
    from tests import env_leak_guard  # noqa: PLC0415

    before = env_leak_guard.snapshot()
    yield
    failure = env_leak_guard.report(
        getattr(request.module, "__name__", str(request.node.nodeid)),
        before,
        env_leak_guard.snapshot(),
    )
    assert not failure, failure
