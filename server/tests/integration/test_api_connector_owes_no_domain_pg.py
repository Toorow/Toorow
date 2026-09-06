"""AI-206 -- un connecteur API va d'installe a READY sans configurer de domaine.

POURQUOI CE FICHIER EXISTE, ET POURQUOI IL EST EN INTEGRATION. La chaine
installation -> verification portait SIX verrous, et chacun etait invisible tant
que le precedent tenait. Quatre d'entre eux sont des triggers Postgres : aucun
test hors base ne pouvait les voir, et c'est exactement pour ca que la ligne
d'action item est restee ouverte si longtemps en etant decrite par ses deux
premiers murs seulement.

Les six, dans l'ordre ou ils tombent :

  1. `apply_installation` envoyait TOUT connecteur en DOMAIN_PENDING, avec pour
     geste suivant << configurer le routage de domaine >> -- un geste qui
     n'existe pas pour un connecteur de module.
  2. `app.protect_connector_installation` epinglait `state = 'DOMAIN_PENDING'`
     a l'INSERT : reparer l'appelant seul produisait une ligne refusee.
  3. `run_verification` refusait sans configuration de domaine active.
  4. `app.validate_connector_verification_run_write` refusait une ligne de
     preuve dont le `domain_config_id` ne correspond a aucune config active --
     et un NULL ne correspond a rien.
  5. `run_verification` passait l'IDENTITE de l'appelant en `responsible_actor`,
     alors que le validateur n'accepte que trois classes et que son propre
     docstring dit << never a subject identity >>. Aucune verification ne
     pouvait donc avancer une installation, pour AUCUN connecteur.
  6. La branche de succes ecrasait la classe de preuve par `routing_check`,
     jetant ce que les controles avaient repondu -- un `auth_check` reussi
     etait inscrit au registre comme un verdict de ROUTAGE.

Le test marche la chaine entiere, parce qu'aucune moitie ne prouve l'autre : les
murs 4, 5 et 6 ne se sont montres qu'apres la reussite des precedents.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_OWNER_DSN"),
    reason="TEST_POSTGRES_OWNER_DSN not set -- live Postgres trigger test skipped",
)


def _conn():
    import psycopg  # noqa: PLC0415

    return psycopg.connect(os.environ["TEST_POSTGRES_OWNER_DSN"], connect_timeout=5)


def _unique(prefix: str) -> str:
    """Un nom neuf par execution : `connector_installations` est append-only, et
    la trappe RGPD ne s'y applique pas -- la table est de portee PLATEFORME et ne
    porte aucune colonne d'org. Rejouer sur le meme nom testerait l'idempotence,
    pas le parcours."""
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}-{ULID()}".lower()


def test_a_module_connector_reaches_ready_with_no_domain_config():
    from core.connector_installation import apply_installation
    from core.connector_verification import run_verification

    # UN VRAI NOM DE MODULE, et un environnement neuf plutot qu'un nom neuf
    # (AI-206, 2026-08-17). `run_verification` ne lit plus l'exigence de domaine
    # sur l'etat transitoire de l'installation : il la demande a
    # `core.connector_family`, qui la demande au registre. Un nom synthetique
    # n'est pas dans le registre, donc il serait classe TRANSPORT et refuse ici
    # pour un domaine qu'il ne doit pas -- les deux derivations doivent etre LA
    # MEME, et un `requires_domain=False` passe a la main pour un nom que
    # personne ne livre etait justement le desaccord qui faisait paraitre le
    # raccourci par l'etat necessaire.
    name = "meta-ads"
    environment = _unique("t206-api")
    conn = _conn()
    try:
        installed = apply_installation(
            conn, environment=environment, connector_name=name,
            responsible_actor=None, blocking_cause=None, actor="person_TEST",
            idempotency_key=f"{environment}-install", host_context={}, trace_id=None,
            requires_domain=False,
        )
        conn.commit()

        # Mur 1 et 2 : il ouvre a VERIFYING, et sans cause bloquante -- un
        # connecteur qui attend un controle automatique n'est pas BLOQUE.
        assert installed["state"] == "VERIFYING"
        assert installed["blocking_cause"] is None
        assert "domain" not in installed["safe_next_action"].lower()

        # Murs 3, 4, 5 et 6 : la preuve d'autorisation se suffit.
        run_verification(
            conn, environment=environment, connector_name=name,
            checks=[lambda: (True, "auth_check", "account discovery reached the provider")],
            actor="person_TEST", idempotency_key=f"{environment}-verify",
            host_context={}, trace_id=None,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, blocking_cause, responsible_actor,"
                " last_verified_at IS NOT NULL"
                " FROM app.connector_installations"
                " WHERE environment = %s AND connector_name = %s",
                (environment, name),
            )
            state, cause, actor_class, verified = cur.fetchone()
            cur.execute(
                "SELECT outcome, evidence_class, domain_config_id"
                " FROM app.connector_verification_runs"
                " WHERE environment = %s AND connector_name = %s",
                (environment, name),
            )
            outcome, evidence_class, domain_config_id = cur.fetchone()
    finally:
        conn.close()

    # L'etat PERSISTE, pas le modele de lecture rendu : c'est la ligne en base
    # qu'un ecran lira demain.
    assert state == "READY"
    assert cause is None
    assert verified is True
    # Mur 5 : une CLASSE d'acteur, jamais le sujet.
    assert actor_class == "automated"
    # Mur 6 : la preuve dit ce qu'elle a prouve.
    assert (outcome, evidence_class) == ("passed", "auth_check")
    # Mur 4 : et elle tient sans contrat de routage.
    assert domain_config_id is None


def test_a_transport_still_owes_its_domain():
    """La moitie qu'il serait tentant d'oublier.

    Elargir un garde sans verifier ce qu'il refuse encore, c'est le desarmer.
    `managed_feed` est le seul membre de la famille entrante, et son etape
    domaine est reelle : elle porte le domaine, l'adaptateur et la preuve DNS.
    """
    from core.connector_installation import apply_installation

    name = _unique("t206-transport")
    conn = _conn()
    try:
        installed = apply_installation(
            conn, environment="preprod", connector_name=name,
            responsible_actor=None, blocking_cause=None, actor="person_TEST",
            idempotency_key=f"{name}-install", host_context={}, trace_id=None,
            requires_domain=True,
        )
        conn.commit()
    finally:
        conn.close()

    assert installed["state"] == "DOMAIN_PENDING"
    assert installed["blocking_cause"] == "domain_configuration_pending"


def test_a_run_with_no_route_may_not_claim_a_routing_verdict():
    """Le trigger 268 refuse un verdict de routage sans route.

    C'est la contrepartie du mur 6 : si la classe de preuve pouvait rester
    `routing_check` sans configuration de domaine, le registre porterait un
    verdict de routage sur une route qui n'existe pas -- et personne ne pourrait
    distinguer les deux apres coup.
    """
    import hashlib  # noqa: PLC0415

    import psycopg  # noqa: PLC0415
    from core.connector_installation import apply_installation
    from ulid import ULID  # noqa: PLC0415

    name = _unique("t206-badclass")
    conn = _conn()
    try:
        apply_installation(
            conn, environment="preprod", connector_name=name,
            responsible_actor=None, blocking_cause=None, actor="person_TEST",
            idempotency_key=f"{name}-install", host_context={}, trace_id=None,
            requires_domain=False,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.connector_installations"
                " WHERE environment = %s AND connector_name = %s",
                ("preprod", name),
            )
            installation_id = cur.fetchone()[0]

        # UNE VRAIE OPERATION, sinon le trigger refuse d'abord sur sa
        # provenance et ce test serait ROUGE POUR LA MAUVAISE RAISON -- il
        # passerait en prouvant qu'une ligne sans operation est rejetee, ce que
        # personne ne met en doute, pendant que la regle visee resterait
        # inexploree. Constate en l'ecrivant.
        operation_id = f"op_{ULID()}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.operations"
                " (id, command_type, actor, effective_org_id, resource_path,"
                "  host_context, versions, request_hash, confirmation_mode,"
                "  idempotency_key_hash)"
                " VALUES (%s, 'connector.verification.ran', 'person_TEST', NULL,"
                "         %s::jsonb, '{}'::jsonb, '{}'::jsonb, %s, 'server', %s)",
                # DE VRAIS SHA-256, derives de l'id. Deux raisons, toutes deux
                # constatees en l'ecrivant : `idempotency_key_hash` est unique,
                # donc deux constantes en dur faisaient echouer le SECOND
                # passage sur une collision -- vert une fois, rouge ensuite ; et
                # la colonne porte un CHECK de forme hexadecimale, qu'une chaine
                # bricolee a partir d'un ULID ne satisfait pas.
                (operation_id,
                 '["platform:connector-verification-runs"]',
                 hashlib.sha256(f"request:{operation_id}".encode()).hexdigest(),
                 hashlib.sha256(f"idempotency:{operation_id}".encode()).hexdigest()),
            )
        conn.commit()

        with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as excinfo:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.connector_verification_runs"
                    " (id, installation_id, domain_config_id, environment,"
                    "  connector_name, outcome, evidence_class, evidence_hash,"
                    "  ttl_seconds, synthetic_delivery, operation_id)"
                    " VALUES (%s, %s, NULL, %s, %s, 'passed', 'routing_check',"
                    "         %s, 3600, false, " + f"'{operation_id}')",
                    # Un id de la forme que la table exige : `ck_..._id` la
                    # contraint, et un id malforme ferait tomber ce test sur la
                    # MAUVAISE contrainte -- vert pour la mauvaise raison.
                    (f"cvr_{ULID()}", installation_id, "preprod", name, "a" * 64),
                )
            conn.commit()
        assert "evidence classification is invalid" in str(excinfo.value)
    finally:
        conn.rollback()
        conn.close()
