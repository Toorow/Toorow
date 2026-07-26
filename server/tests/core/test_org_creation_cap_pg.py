"""Le plafond d'organisation, prouve contre la VRAIE base.

`test_org_creation_cap.py` couvre la logique de branchement avec des curseurs
simules -- rapide, et suffisant pour verifier que le code prend le bon chemin.
Ce qu'il ne peut pas montrer, c'est qu'une DEUXIEME organisation existe
reellement apres l'exemption : mock ou pas, la seule preuve d'une creation est
une ligne en base.

Les deux moities comptent autant :
  - un utilisateur normal est plafonne a la sienne ;
  - un super-admin peut en creer plusieurs, parce qu'il onboarde des clients.

Une exemption qui laisserait passer l'appel sans rien creer serait un faux vert.
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

ONBOARDER = "onboarder@toorow.io"


def _post_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = {}
    return req


def _drop_orgs(identity: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM app.org_members WHERE identity = %s", (identity,)
            )
            org_ids = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM app.org_members WHERE identity = %s", (identity,))
            for org_id in org_ids:
                cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        conn.commit()


@pytest.fixture
def onboarder():
    _drop_orgs(ONBOARDER)
    try:
        yield ONBOARDER
    finally:
        _drop_orgs(ONBOARDER)


async def _create(name: str) -> tuple[int, dict]:
    from core.admin_api import _create_org

    slug = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"
    with (
        patch("core.admin_api._check_auth", return_value=(True, ONBOARDER)),
        patch("core.admin_api._check_invitation_identity",
              new=AsyncMock(return_value=(True, ONBOARDER))),
    ):
        resp = await _create_org(_post_request({"name": name, "slug": slug}))
    return resp.status_code, json.loads(resp.body)


def _org_count(identity: str) -> int:
    from core.db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.org_members WHERE identity = %s AND status = 'active'",
            (identity,),
        )
        return cur.fetchone()[0]


@pg_available
@pytest.mark.anyio
async def test_a_normal_user_is_capped_at_their_own_organization(onboarder):
    """La deuxieme creation est refusee, et RIEN n'est ecrit."""
    with patch.dict(os.environ, {"TOOROW_SUPER_ADMINS": ""}):
        status, body = await _create("Cap first")
        assert status == 201, body

        status, body = await _create("Cap second")
        assert status == 409
        assert body["code"] == "organization_limit_reached"

    # Le refus ne doit pas laisser d'organisation a moitie creee derriere lui.
    assert _org_count(onboarder) == 1


@pg_available
@pytest.mark.anyio
async def test_a_super_admin_really_creates_several_organizations(onboarder):
    """L'exemption CREE, elle ne se contente pas de laisser passer l'appel.

    C'est le cas de l'onboarding : une personne de toorow ouvre l'organisation
    de plusieurs clients. Un 201 sans ligne en base serait un faux vert.
    """
    with patch.dict(os.environ, {"TOOROW_SUPER_ADMINS": ONBOARDER}):
        for label in ("Client A", "Client B", "Client C"):
            status, body = await _create(label)
            assert status == 201, f"{label} refuse : {body}"

    assert _org_count(onboarder) == 3


@pg_available
@pytest.mark.anyio
async def test_the_allow_list_is_case_and_whitespace_tolerant(onboarder):
    """Un email allow-liste avec une casse differente reste super-admin.

    Le contraire ferait perdre son statut a un admin sur une simple majuscule --
    et le symptome serait un 409 incomprehensible, pas un message de droits.
    """
    with patch.dict(os.environ, {"TOOROW_SUPER_ADMINS": f"  {ONBOARDER.upper()}  "}):
        assert (await _create("Client A"))[0] == 201
        assert (await _create("Client B"))[0] == 201

    assert _org_count(onboarder) == 2
