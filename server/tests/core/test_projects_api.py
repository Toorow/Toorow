"""Tests for /api/projects CRUD (Story 7.1, AC3, AC4, AC8).

Runs against the local platform Postgres (default DSN) so the slug UNIQUE
constraint, the FK to app.projects, the archive+revoke flow, and the audit row
are verified against the REAL schema (AI-37: schema-constraint paths need a real
DB, not a mock cursor). Skips when Postgres is unreachable.

Covers the AC8 cases:
  - test_create_project_success
  - test_create_project_duplicate_slug_conflict
  - test_create_project_invalid_timezone
  - test_list_projects_active_only
  - test_patch_project_updates_fields
  - test_delete_project_archives_and_revokes
  - test_delete_already_archived_returns_409
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
    """Probe the opt-in live database without hanging test collection."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

# NOT `anonymous`, AND THAT IS NOT A LOOSENING. The comment that stood here said
# the literal `anonymous` was "the single documented way through in auth-disabled
# mode". It was never that: `resolve_strict_resource_access` refuses `anonymous`
# in the SAME line it refuses everything else while the mode is `disabled`
# (`if not identity or identity == "anonymous" or mode == "disabled"` ->
# `production_identity_required`). And on 2026-08-17 the sentinel stopped being
# storable at all -- migration 270, `no_sentinel_can_be_a_member`, whose CHECK
# `org_members_identity_is_a_person` refuses `anonymous` and `system` -- so the
# `caller_org` fixture below could no longer enrol its own caller and every test
# in this file errored at setup.
#
# The way through is the one the product uses in service and the harness already
# names: `TOOROW_AUTH_MODE=oauth` plus a real membership row, which is what
# `tests.conftest.production_auth_mode` exists for. The identity is minted per
# session so the shared disposable base never makes two of them.
_TEST_IDENTITY = f"owner-{uuid.uuid4().hex[:12]}@example.com"
_AUTH = ("core.admin_api._check_auth", (True, _TEST_IDENTITY))


def _post_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get_request(path_params: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.path_params = path_params or {}
    return req


def _patch_request(project_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _delete_request(project_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    return req


def _drop_project(project_id: str) -> None:
    """Same walker as `_drop_by_slug` -- see its docstring for why, not a list."""
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
        if row is None:
            return
        _purge_org(conn, row[0])


def _preferences(project_id: str) -> tuple:
    """(currency, timezone, currency_origin, timezone_origin) as actually stored."""
    from core.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_currency, reporting_timezone, canonical_currency_origin, "
            "reporting_timezone_origin FROM app.project_preferences WHERE project_id = %s",
            (project_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"aucune préférence écrite pour {project_id}"
    return row


def _assert_default_preferences(project_id: str) -> None:
    currency, timezone_str, currency_origin, timezone_origin = _preferences(project_id)
    assert currency == "EUR"
    assert timezone_str
    # `default` et non `explicit` : personne n'a choisi, et la distinction est le
    # point même de la colonne d'origine.
    assert currency_origin == "default"
    assert timezone_origin == "default"


def _purge_org(conn, org_id: str) -> None:
    """Erase an org's whole tree the way `DELETE /api/organizations` does."""
    from tests.conftest import purge_fixture_org

    try:
        purge_fixture_org(conn, org_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _drop_by_slug(slug: str) -> None:
    """Erase a test project by WALKING its FKs, not by listing three tables.

    The hand-written list -- tenant_key_audit, alert_firings, connection_ref --
    was correct on the day it was written and silently wrong after that. Each new
    table hanging off a project made the final `DELETE FROM app.projects` raise IN
    THE CLEANUP, after the assertions had passed, so the test reported a failure
    it never suffered. The one that did it on 2026-08-05:

        ForeignKeyViolation: ... viole la contrainte
        « fk_project_preferences_project » de la table « project_preferences »

    `purge_org_tree` reads the FK graph from the catalog, so a table added
    tomorrow is covered without anyone remembering this file. Every project here
    is created inside the caller's own throwaway org (`caller_org`), so purging
    that org erases the project and everything beneath it.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE slug = %s", (slug,))
            row = cur.fetchone()
        if row is None:
            return
        _purge_org(conn, row[0])


@pytest.fixture(autouse=True)
def caller_org(production_auth_mode):
    """L'identité de test appartient à une organisation, comme un vrai utilisateur.

    Sans ce contexte, ces tests créaient des projets ORPHELINS : sans org_id, un
    projet n'a pas d'entrepôt où atterrir (warehouse_tenancy retombe sur le
    nommage legacy) et échappe au cloisonnement. La création de projet refuse
    désormais de fabriquer cet objet incohérent, et le harnais doit refléter le
    parcours actual — on crée son organisation, PUIS son premier projet.

    UNE SEULE, ET LA SIENNE. `_AUTH` est `anonymous` pour tout ce fichier (voir
    son commentaire), la base jetable est PARTAGÉE, et `_create_project` choisit
    l'organisation du caller : une adhérence `anonymous` laissée par n'importe
    quoi d'autre en fait deux, et chaque création répond `422 org_required` sur
    un chemin nominal. Mesuré le 2026-08-05 — une sonde manuelle avait laissé une
    ligne, et huit tests sont devenus rouges sans qu'une seule d'entre eux ait
    changé. Le décor se pose donc à partir d'un état connu plutôt que sur ce que
    la base contenait.
    """
    if not _pg_reachable():
        yield None
        return

    from core.db import get_connection

    org_id = f"org_test_{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.org_members WHERE identity = %s", (_AUTH[1][1],)
            )
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (org_id, f"Test {org_id}", org_id.replace("_", "-"), _AUTH[1][1]),
            )
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (f"omem_test_{uuid.uuid4().hex[:12]}", org_id, _AUTH[1][1]),
            )
        conn.commit()
    try:
        yield org_id
    finally:
        # Same walker as production: a project the test left behind hangs off
        # this org, and a bare DELETE on `organizations` trips its FKs.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                still_there = cur.fetchone() is not None
            if still_there:
                _purge_org(conn, org_id)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_project_attaches_org_and_enrols_owner(caller_org):
    """Un projet naît rattaché à son org, et son créateur y accède vraiment.

    Les deux moitiés comptent autant : sans org_id les datasets provisionnés à
    la création de l'organisation ne serviraient jamais ; et sans accès résolu,
    le créateur perd son propre projet dans la seconde qui suit (404 sur ses
    datastreams juste après un 201).

    La seconde moitié interrogeait `app.project_members`. Cette table N'EXISTE
    PLUS -- la migration 132 a versé ses lignes dans `app.resource_grants`
    (scope_type='project') puis l'a supprimée, et l'accès d'un propriétaire d'org
    ne passe plus par une ligne par projet du tout : `_list_projects` le résout
    par `m.role = 'owner'`. La requête levait donc `UndefinedTable`, et une
    réécriture qui viserait `resource_grants` mesurerait tout aussi mal, car pour
    ce créateur-là il n'y a légitimement AUCUNE ligne.

    L'accès se demande donc au produit plutôt qu'à une table : le projet doit
    apparaître dans SA liste. C'est exactement le 404-après-201 que la docstring
    redoute, et c'est falsifiable quelle que soit la table du mois.
    """
    from core.db import get_connection
    from core.projects_api import _create_project, _list_projects  # noqa: PLC0415

    slug = f"attach-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(_post_request({"name": "Attach", "slug": slug}))
        assert resp.status_code == 201
        project_id = json.loads(resp.body)["id"]

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            assert cur.fetchone()[0] == caller_org

        with patch(_AUTH[0], return_value=_AUTH[1]):
            listing = await _list_projects(_get_request())
        assert listing.status_code == 200
        assert project_id in {p["id"] for p in json.loads(listing.body)["projects"]}, (
            "le créateur ne retrouve pas son propre projet"
        )
    finally:
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_without_org_is_refused(caller_org):
    """Sans organisation, la création est REFUSÉE — pas dégradée en orphelin.

    « Le temps de l'appel » n'était pas tenu : la suppression n'était jamais
    défaite. Toutes ces identités valent `anonymous` (voir `_AUTH`), donc ce test
    laissait le caller SANS org et le suivant recevait un
    `422 org_required` sur un chemin qu'il croyait nominal -- un échec importé,
    et qui ne se déclenche que dans cet ordre-là. Les lignes sont rendues dans un
    `finally`.
    """
    from core.db import get_connection
    from core.projects_api import _create_project  # noqa: PLC0415

    identity = _AUTH[1][1]
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, org_id, role, status, joined_at FROM app.org_members "
            "WHERE identity = %s",
            (identity,),
        )
        removed = cur.fetchall()
        cur.execute("DELETE FROM app.org_members WHERE identity = %s", (identity,))
        conn.commit()

    slug = f"noorg-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(_post_request({"name": "NoOrg", "slug": slug}))
        assert resp.status_code == 422
        assert json.loads(resp.body)["code"] == "org_required"

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM app.projects WHERE slug = %s", (slug,))
            assert cur.fetchone() is None, "un projet a été créé malgré le refus"
    finally:
        _drop_by_slug(slug)
        with get_connection() as conn:
            with conn.cursor() as cur:
                for member_id, org_id, role, status, joined_at in removed:
                    cur.execute(
                        "INSERT INTO app.org_members "
                        "(id, org_id, identity, role, status, joined_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (member_id, org_id, identity, role, status, joined_at),
                    )
            conn.commit()


@pg_available
@pytest.mark.anyio
async def test_create_project_success():
    from core.projects_api import _create_project  # noqa: PLC0415

    slug = f"acme-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(_post_request({"name": "Acme", "slug": slug}))
        assert resp.status_code == 201
        body = json.loads(resp.body)
        assert body["id"].startswith("proj_")
        assert body["slug"] == slug
        assert body["status"] == "active"
        # LA DEVISE A DÉMÉNAGÉ, elle n'a pas disparu. `currency` n'est plus une
        # colonne de `app.projects` ni un champ de la réponse : c'est
        # `app.project_preferences.canonical_currency`, avec sa provenance
        # (`..._origin`), pour que « EUR par défaut » et « EUR choisi » cessent
        # de se ressembler. L'assertion suit la valeur là où elle vit, plutôt
        # que de réclamer un champ que l'architecture a retiré.
        _assert_default_preferences(body["id"])
    finally:
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_auto_slug_from_name():
    from core.projects_api import _create_project  # noqa: PLC0415

    name = f"Acme Corp {uuid.uuid4().hex[:6]} (FR)"
    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(_post_request({"name": name}))
    assert resp.status_code == 201
    body = json.loads(resp.body)
    try:
        # lowercase, spaces->hyphens, special chars stripped
        assert body["slug"].startswith("acme-corp-")
        assert body["slug"].endswith("-fr")
    finally:
        _drop_project(body["id"])


@pg_available
@pytest.mark.anyio
async def test_create_project_duplicate_slug_conflict():
    from core.projects_api import _create_project  # noqa: PLC0415

    slug = f"dupe-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    created_ids = []
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r1 = await _create_project(_post_request({"name": "First", "slug": slug}))
            assert r1.status_code == 201
            created_ids.append(json.loads(r1.body)["id"])
            r2 = await _create_project(_post_request({"name": "Second", "slug": slug}))
        assert r2.status_code == 409
    finally:
        for pid in created_ids:
            _drop_project(pid)
        _drop_by_slug(slug)


@pg_available
@pytest.mark.anyio
async def test_create_project_invalid_timezone():
    from core.projects_api import _create_project  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(_post_request({"name": "Bad TZ", "timezone": "Mars/Olympus"}))
    assert resp.status_code == 422


@pg_available
@pytest.mark.anyio
async def test_create_project_invalid_currency():
    from core.projects_api import _create_project  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(_post_request({"name": "Bad Cur", "currency": "XYZ"}))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List / Get
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_list_projects_active_only():
    from core.projects_api import _create_project, _delete_project, _list_projects  # noqa: PLC0415

    active_slug = f"active-{uuid.uuid4().hex[:8]}"
    arch_slug = f"arch-{uuid.uuid4().hex[:8]}"
    created = []
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            ra = await _create_project(_post_request({"name": "ZZZ Active", "slug": active_slug}))
            rb = await _create_project(_post_request({"name": "ZZZ Arch", "slug": arch_slug}))
            active_id = json.loads(ra.body)["id"]
            arch_id = json.loads(rb.body)["id"]
            created = [active_id, arch_id]
            # Archive the second one.
            await _delete_project(_delete_request(arch_id))

            resp = await _list_projects(_get_request())
        assert resp.status_code == 200
        slugs = {p["slug"] for p in json.loads(resp.body)["projects"]}
        assert active_slug in slugs
        assert arch_slug not in slugs
    finally:
        for pid in created:
            _drop_project(pid)


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_patch_project_updates_fields():
    from core.projects_api import _create_project, _patch_project  # noqa: PLC0415

    slug = f"patch-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Before", "slug": slug}))
            pid = json.loads(r.body)["id"]
            resp = await _patch_project(_patch_request(pid, {"name": "After"}))
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["name"] == "After"
        # slug is immutable.
        assert body["slug"] == slug
        # `currency` WAS PATCHED HERE AND SILENTLY IGNORED. `_patch_project`
        # builds its SET clause from `name`, `description`, the verification
        # source fields and the geographic posture -- `currency` is in none of
        # them, so the old assertion `body["currency"] == "USD"` could only ever
        # have read a field the response no longer carries. The devise lives in
        # `app.project_preferences` and is changed by its own surface, with
        # provenance; asserting it is UNCHANGED here is the honest statement, and
        # it fails the day someone wires a currency write into this handler
        # without a provenance decision.
        currency, _, currency_origin, _ = _preferences(pid)
        assert (currency, currency_origin) == ("EUR", "default")
    finally:
        if pid:
            _drop_project(pid)


# ---------------------------------------------------------------------------
# Delete (archive) + revoke connections + audit
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_delete_project_archives_and_revokes():
    from core.db import get_connection
    from core.projects_api import _create_project, _delete_project  # noqa: PLC0415

    slug = f"del-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "To Delete", "slug": slug}))
            pid = json.loads(r.body)["id"]

        # Attach a connection to the project.
        conn_id = f"conn_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.connection_ref "
                    "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                    "VALUES (%s, 'google-analytics', %s, %s, 'org_test_fixture', "
                    "'tester@example.com')",
                    (conn_id, conn_id, pid),
                )
            conn.commit()

        with (
            patch(_AUTH[0], return_value=_AUTH[1]),
            patch("core.projects_api.write_audit_row") as mock_audit,
        ):
            resp = await _delete_project(_delete_request(pid))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["status"] == "archived"
        assert body["connections_revoked"] == 1

        # DB assertions: project archived, connection revoked.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, archived_at FROM app.projects WHERE id = %s", (pid,))
                prow = cur.fetchone()
                assert prow[0] == "archived"
                assert prow[1] is not None
                cur.execute(
                    "SELECT status, revoked_at FROM app.connection_ref WHERE id = %s",
                    (conn_id,),
                )
                crow = cur.fetchone()
                assert crow[0] == "revoked"
                assert crow[1] is not None

        # Audit rows: Story 7.3 adds a key_deleted audit row BEFORE project_archived.
        # The archive handler now calls write_audit_row twice:
        #   1. action='key_deleted'   (Story 7.3, T5.2)
        #   2. action='project_archived' (Story 7.1, AC4)
        assert mock_audit.call_count >= 1
        all_calls = mock_audit.call_args_list
        actions = [c.kwargs.get("action") for c in all_calls]
        assert "project_archived" in actions, (
            f"project_archived missing from audit calls: {actions}"
        )
        archive_call = next(c for c in all_calls if c.kwargs.get("action") == "project_archived")
        assert archive_call.kwargs["metadata"]["connections_revoked"] == 1
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_delete_already_archived_returns_409():
    from core.projects_api import _create_project, _delete_project  # noqa: PLC0415

    slug = f"twice-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Twice", "slug": slug}))
            pid = json.loads(r.body)["id"]
            first = await _delete_project(_delete_request(pid))
            assert first.status_code == 200
            second = await _delete_project(_delete_request(pid))
        assert second.status_code == 409
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_an_archived_project_is_visible_and_restorable():
    """AI-363, ratified 2026-09-02 (project-settings.md).

    The archive is not a disappearance: the identity that may see the project
    still reads it (status archived), and POST /restore -- the gesture the 409
    of a second DELETE names -- brings it back to active. Connections stay
    revoked: restoring is not re-consenting.
    """
    from core.projects_api import (  # noqa: PLC0415
        _create_project,
        _delete_project,
        _get_project,
        _restore_project,
    )

    slug = f"undo-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Undo", "slug": slug}))
            pid = json.loads(r.body)["id"]

            assert (await _delete_project(_delete_request(pid))).status_code == 200

            seen = await _get_project(_get_request({"project_id": pid}))
            assert seen.status_code == 200
            body = json.loads(seen.body)
            assert body["status"] == "archived"
            assert body["archived_at"] is not None

            restored = await _restore_project(_delete_request(pid))
            assert restored.status_code == 200
            assert json.loads(restored.body)["status"] == "active"

            back = json.loads((await _get_project(_get_request({"project_id": pid}))).body)
            assert back["status"] == "active"
            assert back["archived_at"] is None

            # The full cycle: it can be archived again, through the same door.
            assert (await _delete_project(_delete_request(pid))).status_code == 200
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_restoring_an_active_project_is_a_409_not_a_write():
    from core.projects_api import _create_project, _restore_project  # noqa: PLC0415

    slug = f"norestore-{uuid.uuid4().hex[:8]}"
    _drop_by_slug(slug)
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "NoRestore", "slug": slug}))
            pid = json.loads(r.body)["id"]
            resp = await _restore_project(_delete_request(pid))
        assert resp.status_code == 409
        assert json.loads(resp.body)["code"] == "conflict"
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_delete_missing_project_returns_404():
    from core.projects_api import _delete_project  # noqa: PLC0415

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _delete_project(_delete_request("proj_does_not_exist"))
    assert resp.status_code == 404
