"""Shared pytest fixtures for the server test suite.

Provides the ``live_postgres`` fixture (AI-37, Story 6.1, AC14): a real psycopg
connection to the test Postgres, used to verify schema constraints (NOT NULL / FK /
UNIQUE) that mocked cursors cannot catch. Skips when ``TEST_POSTGRES_DSN`` is unset.
"""

from __future__ import annotations

import os

import pytest

#: Organisation de fixture, partagée par toute la suite.
#:
#: Tout projet appartient à une organisation, SANS EXCEPTION : c'est la chaîne du
#: produit (org -> projet -> datastream, et le credential au niveau org). Les
#: tests qui insèrent un projet directement doivent donc fournir une org, comme
#: n'importe quel appelant. Cette constante leur en donne une, créée une fois par
#: session, plutôt que d'obliger chaque fichier à en fabriquer une.
TEST_ORG_ID = "org_test_fixture"


@pytest.fixture(scope="session", autouse=True)
def test_org():
    """Garantit l'existence de TEST_ORG_ID quand un Postgres réel est branché.

    Idempotente et JAMAIS supprimée en fin de session : plusieurs fichiers de
    test (voire plusieurs sessions en parallèle) s'y rattachent, et la détruire
    ferait tomber les projets des autres. C'est une donnée de socle, pas un
    artefact de test.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        return TEST_ORG_ID

    import psycopg  # noqa: PLC0415

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Test fixture org', 'org-test-fixture', 'active', 'system') "
                    "ON CONFLICT (id) DO NOTHING",
                    (TEST_ORG_ID,),
                )
                # Un membre, sinon l'org est SANS MEMBRE -- donc default-open
                # jusqu'a enrolement (21.5) et visible par n'importe quelle
                # identite. Une org de fixture ne doit pas apparaitre dans le
                # cloisonnement mesure par le harnais.
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES ('omem_test_fixture', %s, 'system', 'owner', 'active', NOW()) "
                    "ON CONFLICT (org_id, identity) DO NOTHING",
                    (TEST_ORG_ID,),
                )
            conn.commit()
    except Exception:
        # Pas de Postgres joignable : les tests pg-gated se sauteront d'eux-mêmes.
        pass
    return TEST_ORG_ID


@pytest.fixture
def live_postgres():
    """Yield a real psycopg connection to the test Postgres (AI-37).

    Skips the test when ``TEST_POSTGRES_DSN`` is not set — CI without a Postgres
    service, and contributors who have not opted in, run everything else. When set,
    the DSN points at a database where the app schema migrations (incl. 014) have
    been applied.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set — skipping live Postgres integration test")

    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
