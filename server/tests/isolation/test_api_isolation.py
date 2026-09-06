"""Admin API isolation — cross-scope attempts rejected AND audited (Story 7.4).

Covers AC4, AC7 (AI-38), AC8. The admin API uses a shared Bearer token, so a
"cross-scope attempt" is simulated by passing a WRONG project_id scope (alpha)
against another project's (beta) notebook. Even with a shared credential the
endpoint must validate ownership: a mismatch returns 404 (existence not
disclosed) and writes an ACTION_CROSS_SCOPE_ATTEMPT audit row.

Runs against live Postgres (TEST_POSTGRES_DSN) so the SQL scoping is proven, not
mocked. Auth is disabled -> identity 'anonymous'; scope enforcement still fires
on the explicit-claim-mismatch path.
"""

from __future__ import annotations

import os

import psycopg
import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

pytestmark = pytest.mark.isolation


@pytest.fixture()
def client(two_projects):
    """Admin router test client with auth disabled and PLATFORM_DB_URL set."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    os.environ["PLATFORM_DB_URL"] = os.environ["TEST_POSTGRES_DSN"]
    from core import api_auth
    from core.admin_api import router

    api_auth.reset_verifier_cache()
    app = Starlette(routes=[Mount("/", app=router)])
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


def _audit_count(action: str, notebook_id: str) -> int:
    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM app.audit_log
                WHERE action = %s AND metadata->>'notebook_id' = %s
                """,
                (action, notebook_id),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def test_reports_available_scoped_to_alpha(client, two_projects):
    """GET /api/reports/available?project_id=alpha reflects ONLY alpha's per-project
    enablement rows (app.project_reports), never beta's.

    The report *catalog* is module-defined and shared, so isolation here is about
    the per-project enablement state merged in: alpha's response must carry
    alpha's project_reports rows and none of beta's. We seeded alpha with a
    google-analytics/overview_daily enablement and beta with a distinct
    gsc/position_movements one; the enablement row for beta's report must not
    surface in alpha's response, and the raw table must be project-scoped.
    """
    alpha, beta = two_projects

    # Direct table scoping proof (this is the AC3 property).
    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT module_name, report_id FROM app.project_reports "
                "WHERE project_id = %s",
                (alpha["id"],),
            )
            alpha_reports = {(m, r) for m, r in cur.fetchall()}
    finally:
        conn.close()

    assert ("google-analytics", "overview_daily") in alpha_reports
    assert ("gsc", "position_movements") not in alpha_reports, (
        "beta's project_reports row leaked into alpha's scope!"
    )

    # Endpoint responds 200 and is project-scoped (does not 500 or cross-read).
    resp = client.get(f"/api/reports/available?project_id={alpha['id']}")
    assert resp.status_code == 200


def test_notebooks_list_scoped_to_alpha(client, two_projects):
    """GET /api/notebooks?project_id=alpha returns only alpha notebooks."""
    alpha, beta = two_projects
    resp = client.get(f"/api/notebooks?project_id={alpha['id']}")
    assert resp.status_code == 200
    payload = resp.json()
    notebooks = payload if isinstance(payload, list) else payload.get("notebooks", [])
    ids = {nb["id"] for nb in notebooks}
    assert alpha["notebook_id"] in ids
    assert beta["notebook_id"] not in ids, "beta notebook leaked into alpha list!"


def test_cross_scope_notebook_get_returns_404(client, two_projects):
    """GET /api/notebooks/{beta_nb}?project_id=alpha -> 404 (AI-38)."""
    alpha, beta = two_projects
    resp = client.get(f"/api/notebooks/{beta['notebook_id']}?project_id={alpha['id']}")
    assert resp.status_code == 404, resp.text
    # Same notebook fetched with its OWN scope succeeds -> proves it EXISTS.
    ok = client.get(f"/api/notebooks/{beta['notebook_id']}?project_id={beta['id']}")
    assert ok.status_code == 200


def test_a_cross_scope_notebook_patch_mutates_NOTHING(client, two_projects):
    """La propriete tenue reste ; le code de refus a change, et pour le mieux.

    Ce test exigeait un **404**, parce que la porte lisait le notebook avant de
    decider et devait refuser sans dire qu'il existait. Depuis le 2026-08-22 la
    porte refuse AVANT de lire : `app.notebooks` est en lecture seule, son seul
    ecrivain (l'outil MCP `save_notebook`) ecrit desormais le magasin gouverne,
    et une ecriture sur l'ancien magasin recoit **409** avec la phrase qui nomme
    la surface qui marche.

    LE REFUS EST DEVENU PLUS FORT, pas plus faible : il est le meme pour tout
    appelant et pour tout identifiant, existant ou non, donc comparer deux refus
    n'apprend RIEN -- ce que le 404 tentait d'obtenir en lisant d'abord.
    """
    alpha, beta = two_projects
    resp = client.patch(
        f"/api/notebooks/{beta['notebook_id']}",
        json={"title": "HIJACKED", "project_id": alpha["id"]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "legacy_store_is_read_only"

    # ET LA LIGNE N'A PAS BOUGE. C'est la moitie du test qui compte, et elle est
    # inchangee : un refus qui muterait quand meme serait le pire des deux.
    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM app.notebooks WHERE id = %s",
                (beta["notebook_id"],),
            )
            title = cur.fetchone()[0]
    finally:
        conn.close()
    assert title != "HIJACKED", "cross-scope PATCH mutated beta's notebook!"


def test_the_refusal_is_INDISTINGUISHABLE_between_a_real_id_and_an_absent_one(
    client, two_projects
):
    """Deux refus compares n'apprennent pas qu'un objet existe.

    C'est la propriete que le 404 d'avant achetait en lisant la ligne d'abord.
    Ici elle est gratuite : la porte ne lit rien.
    """
    _alpha, beta = two_projects
    real = client.patch(f"/api/notebooks/{beta['notebook_id']}", json={"title": "x"})
    absent = client.patch("/api/notebooks/nb_DOES_NOT_EXIST", json={"title": "x"})
    assert real.status_code == absent.status_code == 409
    assert real.json() == absent.json()


def test_a_cross_scope_notebook_schedule_mutates_NOTHING(client, two_projects):
    """Meme porte, meme refus -- et la programmation vit ailleurs desormais.

    Un Notebook gouverne se programme sur la surface Notebooks du Projet, qui
    ecrit `app.analysis_notebook_schedules` ; c'est ce que
    `dispatch_due_notebook_schedules` tire chaque nuit. La boucle nocturne qui
    lisait `app.notebooks.scheduled` a ete retiree le meme jour.
    """
    alpha, beta = two_projects
    resp = client.patch(
        f"/api/notebooks/{beta['notebook_id']}/schedule",
        json={"scheduled": True, "schedule_rule": "nightly", "project_id": alpha["id"]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "legacy_store_is_read_only"

    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scheduled FROM app.notebooks WHERE id = %s",
                (beta["notebook_id"],),
            )
            scheduled = cur.fetchone()[0]
    finally:
        conn.close()
    assert not scheduled, "cross-scope schedule armed beta's notebook!"


def test_cross_scope_notebook_export_returns_404(client, two_projects):
    """GET /api/notebooks/{beta_nb}/runs/{run}/export/html with alpha scope -> 404."""
    alpha, beta = two_projects
    url = (
        f"/api/notebooks/{beta['notebook_id']}/runs/{beta['notebook_run_id']}"
        f"/export/html?project_id={alpha['id']}"
    )
    resp = client.get(url)
    assert resp.status_code == 404, resp.text


def test_cross_scope_attempt_is_audited(client, two_projects):
    """A rejected cross-scope attempt writes an access_denied audit row (AC4).

    LA PORTE A CHANGE, PAS LA PROPRIETE. Ce test passait par `PATCH
    /api/notebooks/{id}` ; depuis le 2026-08-22 cette porte refuse toute ecriture
    AVANT de lire quoi que ce soit (`app.notebooks` est en lecture seule), donc
    il n'y a plus de tentative SCOPEE a auditer sur elle -- il n'y a plus
    d'ecriture du tout.

    AC4 vit sur les portes qui SCOPENT encore, et il en reste deux :
    `_get_notebook` et `_export_notebook_html` appellent toutes deux
    `_enforce_notebook_project_scope`, qui ecrit la ligne d'audit avant de
    refuser. C'est l'export qui est joue ici parce que
    `test_cross_scope_notebook_get_returns_404` est rouge pour une cause
    anterieure et sans rapport ; un test qui prouve une propriete ne doit pas
    dependre d'un rouge voisin.
    """
    from core.audit import ACTION_CROSS_SCOPE_ATTEMPT

    alpha, beta = two_projects
    before = _audit_count(ACTION_CROSS_SCOPE_ATTEMPT, beta["notebook_id"])

    resp = client.get(
        f"/api/notebooks/{beta['notebook_id']}/runs/{beta['notebook_run_id']}"
        f"/export/html?project_id={alpha['id']}"
    )
    assert resp.status_code == 404, resp.text

    after = _audit_count(ACTION_CROSS_SCOPE_ATTEMPT, beta["notebook_id"])
    assert after == before + 1, "cross-scope attempt must be audited (access_denied)"


def test_an_archived_project_is_closed_at_every_door(two_projects, pg_conn):
    """review-epic-7 F-3: archiving closes a project. Asserted at BOTH doors now.

    The original assertion called `project_access.identity_has_project_access`,
    the "default-open until the project has members" resolver. That model is
    gone: the ratified spine says *"No explicit grant means no access; the owner
    floor is explicit [...] no other role inherits access from zero grants"*
    (quoted in `docs/product-architecture/alignment-register.md` section 7), and
    `resolve_strict_resource_access` implements it -- so there is no name left
    for the function this test imported.

    What the clause actually protects is UNCHANGED and is what is asserted here:
    an archived project answers no, whichever door the caller arrives at. Both
    are checked because they are genuinely different code, and the one with the
    auth-disabled developer branch is the one an archived project could slip
    through: `_strict_project_capability_allowed` grants every capability to
    `anonymous` in `make dev`, and only `project_exists`' `status = 'active'`
    clause stops it doing so on an archived row.
    """
    from core.admin_api import _strict_project_capability_allowed
    from core.project_access import identity_can_read_project

    beta_id = "proj_beta"
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE app.projects SET status = 'archived' WHERE id = %s", (beta_id,))
    pg_conn.commit()
    try:
        assert identity_can_read_project(beta_id, "any-user", pg_conn) is False
        assert (
            _strict_project_capability_allowed(
                pg_conn,
                identity="anonymous",
                project_id=beta_id,
                minimum_capability="view",
            )
            is False
        )
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE app.projects SET status = 'active' WHERE id = %s", (beta_id,))
        pg_conn.commit()
