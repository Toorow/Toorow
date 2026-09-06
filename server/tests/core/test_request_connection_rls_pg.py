"""Story 21.6 AC4 -- the epic's Given/When/Then, executed in raw SQL.

    Given RLS active and a session bound to org Acme
    When a query reads a row of org Globex directly in SQL
    Then RLS returns zero rows.

DELIBERATELY WITHOUT `access.py`. Every handler adds `WHERE org_id = %s`, and the
reviewer can confirm it is there -- which is exactly why it cannot be what is
under test. The statements below carry no scope predicate at all. They ask the
database, and nothing else, to withhold the row.

IT NEVER SKIPS INTO GREEN. A superuser and a BYPASSRLS role both ignore RLS, so
an isolation assertion under either passes while proving nothing. This file FAILS
in that case instead of skipping, and every assertion is paired with a negative
control -- the same statement with the floor down must return the row -- so an
empty result for an unrelated reason cannot be mistaken for isolation.
"""

from __future__ import annotations

import os

import pytest
from core.db import background_connection, request_connection
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _assert_rls_can_bite(cur) -> None:
    cur.execute(
        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user"
    )
    user, is_super, bypasses = cur.fetchone()
    assert not is_super, (
        f"connected as superuser {user!r}: RLS is bypassed and this file would "
        "pass without proving anything"
    )
    assert not bypasses, f"role {user!r} has BYPASSRLS: this file proves nothing"


class TwoOrganizations:
    """Acme and Globex, and a person who is an active member of Acme only."""

    def __init__(self, conn):
        self.conn = conn
        self.acme = _uid("org")
        self.globex = _uid("org")
        self.acme_project = _uid("proj")
        self.globex_project = _uid("proj")
        self.subject = f"person_{ULID()}"

    def build(self) -> TwoOrganizations:
        with self.conn.cursor() as cur:
            for org_id, label in ((self.acme, "Acme"), (self.globex, "Globex")):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, %s, %s, 'active', 'test')",
                    (org_id, f"21.6 fixture {label}", org_id.replace("_", "-")),
                )
            for project_id, org_id in (
                (self.acme_project, self.acme),
                (self.globex_project, self.globex),
            ):
                cur.execute(
                    "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                    "VALUES (%s, %s, '21.6 fixture', %s, 'test')",
                    (project_id, org_id, project_id.replace("_", "-")),
                )
            # An active MEMBER of Acme, not an owner: an owner short-circuits
            # `epic36_has_resource_access`, and the grant below is what carries
            # the capability.
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status) "
                "VALUES (%s, %s, %s, 'member', 'active')",
                (_uid("om"), self.acme, self.subject),
            )
            cur.execute(
                "INSERT INTO app.resource_grants "
                "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
                "VALUES (%s, %s, %s, 'project', %s, 'view', 'test')",
                (_uid("rg"), self.acme, self.subject, self.acme_project),
            )
        self.conn.commit()
        return self


@pytest.fixture()
def two_orgs(live_postgres):
    return TwoOrganizations(live_postgres).build()


@pytest.fixture()
def platform_db_is_the_disposable_one(monkeypatch):
    """Point `get_connection` at the same disposable database the fixture used.

    Without this the seam would open the developer's default DSN, and the test
    would assert against a database that has none of the rows above.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")


def test_a_foreign_org_row_is_invisible_through_the_request_seam(
    two_orgs, platform_db_is_the_disposable_one
):
    """AC4, literally: bound to Acme, read Globex, get nothing."""
    with request_connection(two_orgs.subject) as conn, conn.cursor() as cur:
        _assert_rls_can_bite(cur)
        cur.execute("SELECT count(*) FROM app.projects WHERE id = %s", (two_orgs.globex_project,))
        foreign = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM app.projects WHERE id = %s", (two_orgs.acme_project,))
        own = cur.fetchone()[0]

    assert foreign == 0, (
        "a member of Acme read a project row of Globex in raw SQL: the floor is down"
    )
    assert own == 1, (
        "the caller's OWN project became unreadable too -- that is a blanket "
        "denial, not isolation, and it would break every legitimate path"
    )


def test_the_negative_control_shows_the_row_is_actually_there(
    two_orgs, platform_db_is_the_disposable_one
):
    """Same statement, floor down. 1 row -- so the 0 above means isolation."""
    with background_connection("negative control: prove the row exists") as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.projects WHERE id = %s",
                (two_orgs.globex_project,),
            )
            assert cur.fetchone()[0] == 1, (
                "the Globex row is absent, so the isolation assertion is vacuous"
            )


def test_the_floor_survives_a_commit_in_the_middle_of_the_request(
    two_orgs, platform_db_is_the_disposable_one
):
    """The trap this story exists to close, proved on real Postgres.

    A command handler writes, commits, and keeps reading. With the old
    transaction-local context the commit reset `toorow.enforce_epic36` and every
    statement after it ran with the floor down -- silently, halfway through a
    request that had passed its access check.
    """
    with request_connection(two_orgs.subject) as conn, conn.cursor() as cur:
        _assert_rls_can_bite(cur)
        cur.execute("SELECT count(*) FROM app.projects WHERE id = %s", (two_orgs.globex_project,))
        assert cur.fetchone()[0] == 0

        conn.commit()

        cur.execute("SELECT count(*) FROM app.projects WHERE id = %s", (two_orgs.globex_project,))
        after_commit = cur.fetchone()[0]
        cur.execute(
            "SELECT current_setting('toorow.enforce_epic36', true), "
            "current_setting('toorow.identity', true)"
        )
        enforce, identity = cur.fetchone()

    assert (enforce, identity) == ("on", two_orgs.subject), (
        "the access context did not survive the handler's commit: it reverted to "
        f"{enforce!r}/{identity!r}, which makes every policy permissive"
    )
    assert after_commit == 0, "the foreign row became readable after a commit"


def test_the_floor_survives_a_rollback_in_the_middle_of_the_request(
    two_orgs, platform_db_is_the_disposable_one
):
    """A handler that catches an error and rolls back keeps its floor.

    This is why the context is committed at acquisition: a session SET installed
    inside the request's own transaction would be undone by that rollback.
    """
    with request_connection(two_orgs.subject) as conn, conn.cursor() as cur:
        _assert_rls_can_bite(cur)
        try:
            cur.execute("SELECT 1 FROM app.this_relation_does_not_exist")
        except Exception:
            conn.rollback()

        cur.execute("SELECT count(*) FROM app.projects WHERE id = %s", (two_orgs.globex_project,))
        after_rollback = cur.fetchone()[0]
        cur.execute("SELECT current_setting('toorow.enforce_epic36', true)")
        enforce = cur.fetchone()[0]

    assert enforce == "on", (
        f"the access context was rolled back with the handler's transaction ({enforce!r})"
    )
    assert after_rollback == 0, "the foreign row became readable after a rollback"


def test_a_background_connection_is_not_isolated_and_that_is_the_decision(
    two_orgs, platform_db_is_the_disposable_one
):
    """AC3, proved rather than asserted in a comment.

    The nightly dispatch legitimately reads every organization's Datastreams.
    This test exists so that the day someone decides background paths SHOULD
    isolate, it fails and names the choice being changed.
    """
    with background_connection("nightly dispatch reads every org") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('toorow.enforce_epic36', true)")
            assert cur.fetchone()[0] in (None, "", "off")
            cur.execute(
                "SELECT count(*) FROM app.projects WHERE id IN (%s, %s)",
                (two_orgs.acme_project, two_orgs.globex_project),
            )
            assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# The deployment assumption, made falsifiable.
# ---------------------------------------------------------------------------


def test_a_connection_that_forgets_its_context_is_REFUSED_not_served():
    """Ce que ferait un pooler en mode TRANSACTION, et ce qu'on repond.

    Tout le design du plancher tient sur une hypothese que rien ne verifiait :
    une connexion cliente garde son backend. Mesure du 2026-08-05 en production
    -- `application_name='Supavisor'` -- l'application parle a Postgres A TRAVERS
    UN POOLER. En mode session l'hypothese tient ; en mode transaction le backend
    est rendu apres chaque transaction, donc le reglage de session pose a
    l'acquisition a disparu a l'instruction suivante, et le backend qui le
    portait part a quelqu'un d'autre.

    Rien ne le detectait : les politiques laissent tout passer exactement comme
    aujourd'hui, seule la raison change. Ce fichier lui-meme ne pouvait pas le
    voir, parce qu'une base jetable n'a pas de pooler devant elle.

    Le comportement d'un pooler transactionnel est donc SIMULE ici par son effet
    observable : la connexion suivante ne porte plus les reglages de session.
    `RESET ALL` produit exactement cet etat -- `DISCARD ALL` serait plus proche
    du vrai geste de Supavisor mais Postgres le refuse dans un bloc de
    transaction, et ce que le plancher doit detecter est l'ETAT, pas la commande
    qui l'a produit. Si `install_access_context` rendait la main ainsi,
    l'appelant servirait des lignes sans isolation en se croyant arme.
    """
    from core.db import (
        PooledConnectionRefused,
        get_connection,
        install_access_context,
    )

    identity = f"pooled-{ULID()}@example.com"

    # 1 -- le cas nominal : le contexte survit a son propre commit.
    with get_connection() as conn:
        install_access_context(conn, identity)
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('toorow.enforce_epic36', true)")
            assert cur.fetchone()[0] == "on"

    # 2 -- le cas du pooler : la session oublie, et l'acquisition REFUSE.
    with get_connection() as conn:
        original_commit = conn.commit

        def commit_then_forget():
            original_commit()
            with conn.cursor() as cur:
                cur.execute("RESET ALL")
            original_commit()

        conn.commit = commit_then_forget
        with pytest.raises(PooledConnectionRefused) as refusal:
            install_access_context(conn, identity)

    message = str(refusal.value)
    assert "did not survive its own commit" in message
    # La refus NOMME la cause probable : sans cela, l'operateur lit
    # « contexte perdu » et cherche dans le code applicatif pendant une heure.
    assert "6543" in message and "session-mode or direct DSN" in message
