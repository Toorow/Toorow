"""Story 53.1, AC6 -- le plancher RLS est la DEUXIEME barriere des outils MCP.

CE QUE LA RELECTURE DU 2026-08-10 A TROUVE. AC6 etait cochee et n'etait pas
livree : `grep -n "request_connection" server/core/main.py` rendait **une seule**
occurrence, celle que le seam de portee ouvre pour sa PROPRE resolution
d'autorisation. Les lectures metier restaient nues -- `_core_db.get_connection()`
dans `list_connectors`, `get_daily_report`, `flows_get`, `flows_upsert`,
`submit_feedback`, `search_context`. Et le defaut portait son propre requisitoire
dans le depot, `server/core/db.py`, docstring de `request_connection` :

    << Arming a *different* connection buys nothing either: the authorization
    check usually opens, uses and closes its own, and the handler then opens a
    fresh unarmed one. >>

C'etait mot pour mot la forme livree.

CE QUE CE FICHIER PROUVE, ET POURQUOI IL RETIRE LA PREMIERE BARRIERE. Un test qui
laisserait le controle applicatif en place mesurerait le controle applicatif : il
serait vert avec un plancher absent. Alors il NEUTRALISE `refuse_unless_
project_scope` et demande a la base, seule, de refuser. C'est exactement la
definition de << deuxieme barriere >> : ce qui reste debout quand la premiere
tombe.

`get_context_hub` est le porteur de la preuve parce que sa lecture touche une
table REELLEMENT sous RLS. Mesure du 2026-08-10 sur la base jetable : 69 tables
d'`app` portent une politique, et parmi celles que les treize outils gardes
lisent, seules `app.projects` et `app.datastreams` en portent une.
`app.context_topics`, `app.procedures`, `app.feedback`, `app.context_events`,
`app.project_modules` et `app.morning_briefings` n'en ont AUCUNE : armer leur
connexion est la bonne acquisition, mais le plancher n'y mord pas encore. C'est
une portee mesuree, pas une garantie generale, et elle est ecrite ici plutot que
sous-entendue.

IL NE PASSE JAMAIS EN VERT PAR ACCIDENT. Un superutilisateur et un role BYPASSRLS
ignorent tous deux RLS ; ce fichier ECHOUE dans ce cas au lieu de sauter, et
chaque assertion est doublee d'un controle negatif -- la meme ligne, plancher
baisse, doit etre visible -- pour qu'un resultat vide pour une autre raison ne
puisse pas se faire passer pour de l'isolation.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from core import main as core_main
from core.db import background_connection
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _assert_rls_can_bite(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
        user, is_super, bypasses = cur.fetchone()
    assert not is_super, (
        f"connecte en superutilisateur {user!r} : RLS est ignore et ce fichier "
        "passerait sans rien prouver"
    )
    assert not bypasses, f"le role {user!r} porte BYPASSRLS : ce fichier ne prouve rien"


class _TwoOrganizations:
    """Acme et Globex, et une personne membre active d'Acme seulement."""

    def __init__(self, conn):
        self.conn = conn
        self.acme = _uid("org")
        self.globex = _uid("org")
        self.acme_project = _uid("proj")
        self.globex_project = _uid("proj")
        self.subject = f"person_{ULID()}"

    def build(self) -> "_TwoOrganizations":
        with self.conn.cursor() as cur:
            for org_id, label in ((self.acme, "Acme"), (self.globex, "Globex")):
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, %s, %s, 'active', 'test')",
                    (org_id, f"53.1 fixture {label}", org_id.replace("_", "-")),
                )
            for project_id, org_id in (
                (self.acme_project, self.acme),
                (self.globex_project, self.globex),
            ):
                cur.execute(
                    "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                    "VALUES (%s, %s, '53.1 fixture', %s, 'test')",
                    (project_id, org_id, project_id.replace("_", "-")),
                )
            # Un MEMBRE actif d'Acme, pas un proprietaire : un proprietaire
            # court-circuite `epic36_has_resource_access`, et c'est le grant
            # ci-dessous qui porte la capacite.
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
    return _TwoOrganizations(live_postgres).build()


@pytest.fixture()
def the_caller_is_an_acme_member(two_orgs, monkeypatch):
    """Le jeton, le DSN jetable, et l'authentification ARMEE.

    Sans `TOOROW_AUTH_MODE=oauth` le carve-out self-host mono-operateur
    s'applique et le plancher n'est jamais pose : le test mesurerait la
    derogation, pas la barriere.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    token = SimpleNamespace(claims={"sub": two_orgs.subject}, client_id="cli")
    monkeypatch.setattr(core_main, "get_access_token", lambda: token)
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid)
    return token


@pytest.fixture()
def the_first_barrier_is_removed(monkeypatch):
    """La garde applicative est neutralisee : seule la base peut encore refuser.

    C'est le geste qui rend ce fichier different de
    `tests/isolation/test_mcp_tool_scope_refusal.py`. Celui-la prouve que le
    controle applicatif refuse ; celui-ci prouve ce qui reste quand ce controle
    n'est plus la -- la seule question a laquelle AC6 repond.
    """
    monkeypatch.setattr(core_main, "_refuse_unless_project_scope", lambda *a, **k: None)


def test_the_floor_alone_hides_a_foreign_project_from_an_mcp_tool(
    two_orgs, the_caller_is_an_acme_member, the_first_barrier_is_removed
):
    """AC6 : garde applicative retiree, la base refuse quand meme.

    `get_context_hub` resout l'organisation du projet par une lecture nue de
    `app.projects` (`context_api._project_org_id`, aucun predicat de portee).
    Armee, cette lecture ne voit pas la ligne de Globex et l'outil s'arrete ;
    non armee -- ce qu'elle etait avant cette reparation -- elle la voit et rend
    a un membre d'Acme le corpus de Globex.
    """
    with pytest.raises(ValueError, match="project organization is unavailable"):
        core_main.get_context_hub(project_id=two_orgs.globex_project)


def test_the_negative_control_shows_the_foreign_row_is_actually_there(
    two_orgs, the_caller_is_an_acme_member
):
    """Sans ce controle, l'assertion ci-dessus serait vide de sens."""
    with background_connection("controle negatif : prouver que la ligne existe") as conn:
        _assert_rls_can_bite(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.projects WHERE id = %s",
                (two_orgs.globex_project,),
            )
            assert cur.fetchone()[0] == 1, (
                "la ligne de Globex est absente : l'assertion d'isolation ne "
                "mesurerait alors que son absence"
            )


def test_the_callers_own_project_is_still_readable(
    two_orgs, the_caller_is_an_acme_member, the_first_barrier_is_removed
):
    """Un refus general n'est pas de l'isolation.

    Sans ce test, une connexion armee pour une identite que personne n'est --
    ou un `SET ROLE` sans droit de lecture -- ferait passer le precedent en
    refusant TOUT, et casserait au passage tous les chemins legitimes.
    """
    result = core_main.get_context_hub(project_id=two_orgs.acme_project)
    assert result is not None
    envelope = result.structured_content
    assert envelope["data"]["project_id"] == two_orgs.acme_project


def test_the_tool_connection_carries_the_access_context(
    two_orgs, the_caller_is_an_acme_member
):
    """Le seam d'acquisition est bien celui que les outils appellent.

    Le test precedent prouve l'EFFET. Celui-ci prouve la CAUSE, et sans lui un
    outil pourrait redevenir `get_connection()` tant que sa lecture ne touche
    aucune table sous politique -- ce qui est le cas de six des tables lues par
    les treize outils gardes (voir le docstring du module).
    """
    from core.db import request_connection

    with request_connection(two_orgs.subject) as conn:
        _assert_rls_can_bite(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_setting('toorow.enforce_epic36', true), "
                "current_setting('toorow.identity', true)"
            )
            assert cur.fetchone() == ("on", two_orgs.subject)


# ---------------------------------------------------------------------------
# LE PLANCHER D'UNE ECRITURE, ET PAS SEULEMENT D'UNE LECTURE -- 2026-08-21.
#
# Le docstring de ce module mesurait, le 2026-08-10, que parmi les tables lues
# par les treize outils gardes seules `app.projects` et `app.datastreams`
# portaient une politique. `app.dimension_labels` en porte une depuis
# `274_a_row_without_an_org_belongs_to_no_tenant.sql:50-55`, et une politique
# sans `WITH CHECK` reutilise son `USING` pour l'INSERT : le plancher gouverne
# donc l'UPSERT autant que la lecture derriere.
#
# CE QUI MANQUAIT N'ETAIT PAS LA POLITIQUE, C'ETAIT L'ARMEMENT.
# `dimension_conformance.set_dimension_label` acquerait par `get_connection()`
# -- une connexion nue -- alors qu'`identity` etait deja son argument. La porte
# MCP au-dessus ne controlait pas la portee `org` du tout : les deux barrieres
# etaient absentes en meme temps, ce qui est la definition d'un trou.
#
# CE TEST RETIRE LA PREMIERE BARRIERE, comme les quatre precedents : il appelle
# la fonction du noyau DIRECTEMENT, sans passer par la porte MCP ni par sa
# garde. Ce qui refuse alors est la base, seule.
# ---------------------------------------------------------------------------


def _label_write(org_id: str, identity: str, dimension: str) -> None:
    from core.dimension_conformance import set_dimension_label

    set_dimension_label(
        canonical_dimension=dimension,
        display_label="a name the neighbour would read",
        scope_level="ORG",
        org_id=org_id,
        project_id=None,
        identity=identity,
    )


def test_the_floor_alone_refuses_a_label_written_into_a_foreign_org(
    two_orgs, the_caller_is_an_acme_member
):
    """AC6, cote ECRITURE : un membre d'Acme ne peut pas nommer chez Globex.

    Sur une connexion nue -- la forme livree jusqu'au 2026-08-21 -- cette ligne
    atterrissait chez Globex et devenait le nom que TOUS ses rendus affichent
    pour cette dimension. Ce n'est pas la fuite d'une donnee, c'est la
    fabrication d'une.
    """
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _label_write(two_orgs.globex, two_orgs.subject, _uid("dim").replace("-", "_"))


def test_the_negative_control_shows_the_same_write_lands_in_the_callers_own_org(
    two_orgs, the_caller_is_an_acme_member
):
    """Un refus general n'est pas de l'isolation.

    Sans ce controle, une connexion armee pour une identite que personne n'est
    ferait passer le test precedent en refusant TOUTE ecriture -- et casserait
    le seul parcours que cette branche sert.
    """
    dimension = _uid("dim").replace("-", "_")
    _label_write(two_orgs.acme, two_orgs.subject, dimension)

    with background_connection("controle negatif : la ligne d'Acme a bien ete ecrite") as conn:
        _assert_rls_can_bite(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.dimension_labels "
                "WHERE org_id = %s AND canonical_dimension = %s",
                (two_orgs.acme, dimension),
            )
            assert cur.fetchone()[0] == 1, (
                "l'ecriture legitime n'a rien pose : le refus ci-dessus ne "
                "mesurerait alors qu'une garde qui refuse tout le monde"
            )
# ---------------------------------------------------------------------------
# 67-1 -- THE OTHER HALF OF THE CLASS: THE MCP SURFACES THAT NEVER ARMED AT ALL.
#
# The four tests above proved the floor for `get_context_hub` and for the
# dimension-label write. Neither of them counted the rest: measured on
# 2026-08-21 by an AST sweep of every module of `server/` that REGISTERS MCP
# tools, **72** acquisitions inside MCP surfaces went through the bare
# `get_connection()`. 49 of them were armed as of this file when it was written;
# the remainder are dated in
# `tests/conformance/test_mcp_surfaces_acquire_an_armed_connection.py` with the
# reason each one is not a repair.
#
# THAT `49` IS HISTORY AND IS MARKED AS SUCH -- the number that describes today
# is the one the sweep prints, not the one written here. Re-measured 2026-08-24
# (chantier 67-1): `sweep_mcp_surfaces()` returns **12** bare acquisitions, all
# twelve declared, and none of them is debt any more -- ten are platform-scope
# or before-an-identity, and two carry a written decision (`main.py:_resolve_
# project`, `notebook_mcp.run_notebook_direct`). The sweep also gained the
# resolution of import ALIASES that day, which is how a thirteenth site
# (`cards_mcp.get_card`, `from core.db import get_connection as _pg`) turned out
# never to have been in the 72 at all.
#
# WHAT THE TWO PAIRS BELOW PROVE, AND WHY THEY ARE PAIRS. Each one removes the
# application barrier entirely -- it calls no MCP tool and no guard, only the
# acquisition seam and the store underneath it -- and then asks the database
# alone. Each is followed by a negative control, because a connection armed for
# an identity nobody is would refuse everything and look exactly like isolation.
#
# THE READ THAT DECIDES THE ORG. `metric_semantics_mcp._assert_project_in_org`
# resolves `SELECT org_id FROM app.projects WHERE id = %s` and refuses when the
# row is absent. Until today it resolved that on a bare connection: a member of
# Acme naming a project of Globex read Globex's `org_id` back, and the org guard
# that runs on the OTHER argument never learned the two disagreed.
#
# THE READ THAT DECIDES A DATASTREAM. `app.datastreams` carries
# `epic36_datastreams_strict`. `dimension_reference_mcp.declare_dimension_reference`
# reads the row before writing `data_role` on it; on a bare connection the read
# saw a neighbour's Datastream, and the only thing standing between that and a
# write into it was the application guard above.
# ---------------------------------------------------------------------------


@pytest.fixture()
def a_datastream_in_each_org(two_orgs):
    """One Datastream for Acme, one for Globex, written without the floor."""
    acme_ds = _uid("ds")
    globex_ds = _uid("ds")
    with background_connection("fixture: seed one Datastream per organization") as conn:
        with conn.cursor() as cur:
            for ds_id, org_id, project_id in (
                (acme_ds, two_orgs.acme, two_orgs.acme_project),
                (globex_ds, two_orgs.globex, two_orgs.globex_project),
            ):
                cur.execute(
                    "INSERT INTO app.datastreams (id, org_id, project_id, name) "
                    "VALUES (%s, %s, %s, %s)",
                    (ds_id, org_id, project_id, "67.1 fixture"),
                )
            # A GRANT ON THE PROJECT DOES NOT CASCADE TO ITS DATASTREAMS, and the
            # negative control below is what said so: `app.epic36_has_resource_
            # access(org, 'flux', id)` looks for a grant whose `scope_type` is
            # exactly `flux`, with no inheritance from the project row. Without
            # this line the caller could not read their OWN Datastream, and the
            # isolation assertion above it would have passed by refusing everyone.
            cur.execute(
                "INSERT INTO app.resource_grants "
                "(id, org_id, identity, scope_type, scope_id, capability, granted_by) "
                "VALUES (%s, %s, %s, 'flux', %s, 'view', 'test')",
                (_uid("rg"), two_orgs.acme, two_orgs.subject, acme_ds),
            )
        conn.commit()
    return acme_ds, globex_ds


def test_the_floor_alone_hides_a_foreign_projects_org_from_the_metric_surface(
    two_orgs, the_caller_is_an_acme_member
):
    """No guard is called here at all -- the database is the only thing refusing."""
    from core.metric_semantics_mcp import _assert_project_in_org

    with pytest.raises(Exception) as refused:
        _assert_project_in_org(
            two_orgs.globex_project, two_orgs.globex, two_orgs.subject
        )
    assert "not_found" in str(refused.value), (
        "the refusal must be the existence-hiding one, not a crash: "
        f"{refused.value!r}"
    )


def test_the_negative_control_shows_the_callers_own_project_still_resolves(
    two_orgs, the_caller_is_an_acme_member
):
    """A seam that refused everyone would pass the test above and serve nobody."""
    from core.metric_semantics_mcp import _assert_project_in_org

    _assert_project_in_org(two_orgs.acme_project, two_orgs.acme, two_orgs.subject)


def test_the_floor_alone_hides_a_foreign_datastream_from_an_armed_acquisition(
    two_orgs, a_datastream_in_each_org, the_caller_is_an_acme_member
):
    """The ACQUISITION is what changed, so the acquisition is what is measured.

    `core.datastreams.get_datastream` is unchanged and takes the connection it
    is handed. Handed the armed one, it cannot see Globex's row; handed the bare
    one -- the shape every MCP surface used until 2026-08-21 -- it can. Both
    halves run here, so this test names the one-line difference rather than
    describing it.
    """
    from core.datastreams import get_datastream
    from core.db import get_connection, request_connection

    _acme_ds, globex_ds = a_datastream_in_each_org

    with request_connection(two_orgs.subject) as armed:
        _assert_rls_can_bite(armed)
        assert (
            get_datastream(globex_ds, two_orgs.globex_project, armed) is None
        ), "the armed acquisition returned a Datastream of another organization"

    with get_connection() as bare:
        assert get_datastream(globex_ds, two_orgs.globex_project, bare) is not None, (
            "the bare acquisition did NOT return the foreign row either: the "
            "assertion above would then be measuring an absent fixture, not the "
            "floor"
        )


def test_the_negative_control_shows_the_callers_own_datastream_is_still_readable(
    two_orgs, a_datastream_in_each_org, the_caller_is_an_acme_member
):
    """Without this, refusing every Datastream would look like isolation."""
    from core.datastreams import get_datastream
    from core.db import request_connection

    acme_ds, _globex_ds = a_datastream_in_each_org

    with request_connection(two_orgs.subject) as armed:
        assert get_datastream(acme_ds, two_orgs.acme_project, armed) is not None, (
            "the armed acquisition hid the caller's OWN Datastream: the tools "
            "built on it would answer 'not found' about their own objects"
        )
