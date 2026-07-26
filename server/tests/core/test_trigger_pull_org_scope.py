"""Qui peut declencher une sync ? (POST /api/connections/{id}/pull)

L'autorisation se resout sur l'ORGANISATION proprietaire du credential, jamais
sur la personne qui l'a branche. Les deux moities comptent :

  - un COLLEGUE de l'org doit pouvoir lancer la sync sans posseder l'acces a la
    source -- c'est toute la raison d'etre de `owner_org_id` ;
  - un ETRANGER a l'org ne doit pas pouvoir toucher le credential.

Avant le garde-fou, ce handler ne verifiait que l'existence du credential :
n'importe quelle identite authentifiee pouvait declencher une sync sur n'importe
quel credential, y compris celui d'une autre organisation.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
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

OWNER = "owner@example.com"
COLLEAGUE = "colleague@example.com"
OUTSIDER = "outsider@example.com"


@pytest.fixture
def credential_in_org():
    """Un credential branche par OWNER, utilisable dans son organisation."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:10]
    org = f"org_pull_{suffix}"
    project = f"proj_pull_{suffix}"
    cred = f"conn_pull_{suffix}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Pull scope', %s, 'active', %s)",
                (org, f"pull-scope-{suffix}", OWNER),
            )
            for identity in (OWNER, COLLEAGUE):
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES (%s, %s, %s, 'member', 'active', NOW())",
                    (f"omem_{uuid.uuid4().hex[:12]}", org, identity),
                )
            cur.execute(
                "INSERT INTO app.projects "
                "(id, name, slug, status, currency, timezone, created_by, org_id) "
                "VALUES (%s, 'Pull scope', %s, 'active', 'EUR', 'Europe/Paris', %s, %s)",
                (project, f"pull-scope-{suffix}", OWNER, org),
            )
            cur.execute(
                "INSERT INTO app.connection_ref "
                "(id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
                "VALUES (%s, 'google-analytics', %s, %s, %s, %s)",
                (cred, f"nango_{suffix}", project, org, OWNER),
            )
        conn.commit()
    try:
        yield cred
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (cred,))
                cur.execute("DELETE FROM app.tenant_key_audit WHERE project_id = %s", (project,))
                cur.execute("DELETE FROM app.projects WHERE id = %s", (project,))
                cur.execute("DELETE FROM app.org_members WHERE org_id = %s", (org,))
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org,))
            conn.commit()


def _pull_request(cred_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"id": cred_id}
    req.body = AsyncMock(return_value=b"")
    return req


@pg_available
@pytest.mark.anyio
async def test_colleague_of_the_org_may_trigger_the_sync(credential_in_org):
    """Le propre de owner_org_id : un autre membre lance la sync."""
    from core.admin_api import _trigger_pull

    with (
        patch("core.admin_api._check_auth", return_value=(True, COLLEAGUE)),
        patch(
            "core.queue.enqueue_pull",
            return_value={"job_id": "job_x", "pull_id": "pull_x", "state": "queued"},
        ),
    ):
        resp = await _trigger_pull(_pull_request(credential_in_org))

    assert resp.status_code == 202, resp.body


@pg_available
@pytest.mark.anyio
async def test_outsider_may_not_trigger_the_sync(credential_in_org):
    """404 et non 403 : l'existence du credential d'autrui ne se divulgue pas."""
    from core.admin_api import _trigger_pull

    enqueue = MagicMock()
    with (
        patch("core.admin_api._check_auth", return_value=(True, OUTSIDER)),
        patch("core.queue.enqueue_pull", enqueue),
    ):
        resp = await _trigger_pull(_pull_request(credential_in_org))

    assert resp.status_code == 404
    # La preuve qui compte : rien n'a ete mis en file malgre le refus.
    enqueue.assert_not_called()
