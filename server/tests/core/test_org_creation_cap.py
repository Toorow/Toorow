"""Une personne crée UNE organisation, la sienne. Au-delà, c'est un admin.

Décision de Jean, 2026-07-25 : « un user peut juste avoir son organisation à ce
stade ; la seule personne qui peut rattacher un user à plusieurs organisations
c'est un admin (comme dans le CRM) ». Le plafond porte sur la CRÉATION, pas sur
l'accès — autoriser plus tard l'accès multi-org ne le remet pas en cause.

Avant ce plafond, ``POST /api/organizations`` ne vérifiait rien au-delà d'un
bearer valide : une même identité pouvait créer des organisations sans limite,
chacune provisionnant ses propres datasets warehouse.

Tests unitaires volontairement : la suite ASGI de admin_api coûte 30-45 s par
test, ce qui la rend inutilisable comme garde rapprochée.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from core import admin_api


class _FakeCursor:
    def __init__(self, count: int | None):
        self._count = count

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        if self._count is None:
            raise RuntimeError("database unreachable")

    def fetchone(self):
        return (self._count,)


class _FakeConn:
    def __init__(self, count: int | None):
        self._count = count

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._count)


def _patch_db(count: int | None):
    """Remplace core.db.get_connection par une connexion qui répond *count*."""
    import core.db

    return patch.object(core.db, "get_connection", lambda: _FakeConn(count))


# ---------------------------------------------------------------------------
# _count_active_memberships
# ---------------------------------------------------------------------------


def test_count_is_none_when_the_database_cannot_answer():
    """None n'est PAS zéro : un compte invérifiable ne doit pas ouvrir la porte."""
    with _patch_db(None):
        assert admin_api._count_active_memberships({"ada@example.com"}) is None


def test_count_returns_zero_without_any_usable_key():
    assert admin_api._count_active_memberships({"", "   "}) == 0


def test_count_matches_on_both_identity_keys():
    """Le sujet du jeton ET l'email vérifié sont interrogés ensemble.

    Les deux écrivains de app.org_members ne stockent pas la même valeur :
    _create_org écrit le SUJET, l'acceptation d'invitation écrit l'EMAIL.
    N'en interroger qu'un laisserait le plafond contournable par quiconque a
    rejoint via une invitation.
    """
    seen: dict = {}

    class _CapturingCursor(_FakeCursor):
        def execute(self, sql, params=None):
            seen["sql"] = sql
            seen["params"] = params

    class _CapturingConn(_FakeConn):
        def cursor(self):
            return _CapturingCursor(1)

    import core.db

    with patch.object(core.db, "get_connection", lambda: _CapturingConn(1)):
        admin_api._count_active_memberships({"sub-123", "Ada@Example.com"})

    assert "LOWER(identity) = ANY" in seen["sql"]
    assert "status = 'active'" in seen["sql"]
    assert set(seen["params"][0]) == {"sub-123", "ada@example.com"}


# ---------------------------------------------------------------------------
# Le gate dans _create_org
# ---------------------------------------------------------------------------


class _Req:
    """Requête minimale : le plafond s'exécute avant toute lecture du corps.

    `valid_body=False` envoie un corps illisible EXPRÈS. Quand le plafond laisse
    passer, le handler enchaîne sur la création réelle, et la boucle de collision
    de slug tourne sans fin contre une base simulée qui répond toujours une ligne
    (le premier jet de ce test s'y est bloqué jusqu'au timeout). Un corps invalide
    fait s'arrêter le handler juste APRÈS le plafond : c'est précisément ce qu'on
    veut prouver — qu'il a été franchi — sans entrer dans ce qui suit.
    """

    headers: dict = {}

    def __init__(self, valid_body: bool = True):
        self._valid = valid_body

    async def body(self):
        return json.dumps({"name": "Acme"}).encode() if self._valid else b"{not json"


def _create(count: int | None, *, identity="sub-123", email="ada@example.com",
            super_admins="", valid_body=True):
    async def _auth(_request):
        return True, identity

    async def _invite_identity(_request):
        return (True, email) if email else (False, "")

    with patch.dict(os.environ, {"TOOROW_SUPER_ADMINS": super_admins}), \
            patch.object(admin_api, "_check_auth", _auth), \
            patch.object(admin_api, "_check_invitation_identity", _invite_identity), \
            _patch_db(count):
        return asyncio.run(admin_api._create_org(_Req(valid_body)))


def _payload(response):
    return json.loads(bytes(response.body).decode())


def test_second_organization_is_refused():
    resp = _create(1)
    assert resp.status_code == 409
    body = _payload(resp)
    assert body["code"] == "organization_limit_reached"
    # Le message doit dire QUOI FAIRE, pas seulement « non ».
    assert "administrator" in body["message"].lower()


def test_unverifiable_membership_count_fails_closed():
    """Une base muette ne crée PAS l'organisation — elle provisionnerait un
    warehouse qu'on ne saurait pas justifier."""
    resp = _create(None)
    assert resp.status_code == 500
    assert _payload(resp)["code"] == "db_error"
    assert "Nothing was created" in _payload(resp)["message"]


def test_super_admin_is_not_capped():
    """L'admin plateforme est précisément celui qui rattache au-delà d'une org.

    Il a 3 adhésions : sans dérogation, le plafond refuserait. On constate qu'on
    atteint l'étape SUIVANTE (lecture du corps) — donc que le plafond a été
    franchi.
    """
    resp = _create(3, identity="ada@example.com", super_admins="ada@example.com",
                   valid_body=False)
    assert resp.status_code == 400
    assert _payload(resp)["code"] == "invalid_body"


def test_super_admin_recognised_on_the_verified_email():
    """L'allow-list est une liste d'emails ; le sujet du jeton peut être opaque.

    Sans cette reconnaissance sur l'email, un admin dont le jeton porte un `sub`
    opaque serait plafonné comme tout le monde.
    """
    resp = _create(3, identity="sub-opaque-999", email="ada@example.com",
                   super_admins="ada@example.com", valid_body=False)
    assert _payload(resp)["code"] == "invalid_body"


def test_first_organization_passes_the_gate():
    """Zéro adhésion : le plafond ne bloque pas — c'est le cas nominal du
    premier login, l'écran « Create your organization »."""
    resp = _create(0, valid_body=False)
    assert resp.status_code == 400
    assert _payload(resp)["code"] == "invalid_body"
