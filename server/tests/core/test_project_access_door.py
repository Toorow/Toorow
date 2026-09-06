# -*- coding: utf-8 -*-
"""La porte MCP d'ACCES -- qui atteint ce Projet, et transmettre un acces.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST CELA. C'etait la ligne la plus NUE
des dix que l'audit de gap du 2026-09-05 a mesurees : *« know who has access to
what, and hand an access on -- **none.** No registered tool names access, a grant
or a handoff. »* Un modele pouvait travailler dans un Projet toute la journee et
ne pas repondre a la premiere question qu'on pose sur un Projet : qui d'autre est
la, et que peut-il faire ?

TROIS PROMESSES, ET AUCUNE NE SE LIT DANS LE CODE :

  1. **les memes fonctions que l'ecran** -- `core.project_access_surface`, les
     cinq que `project_access_api.py` traduit depuis HTTP ;
  2. **le secret est frappe et depense dans UNE transaction, et n'entre jamais
     dans la reponse.** La console fait transiter ce secret par un navigateur
     parce qu'une personne doit confirmer ; ici les trois appels vivent dans une
     transaction serveur, donc le secret est cree et depense sans jamais etre
     ecrit. C'est plus fort que le chemin console, pas une version affaiblie ;
  3. **un transfert rend son objet, jamais son lien.** `mcp-tool-surface.md` le
     dit des Shares dans les memes mots : l'objet est operable par un agent, le
     lien secret ne l'est pas.

La 2 et la 3 sont exactement le genre de promesse qu'une revue ne verra pas et
qu'un test doit tenir. Les deux sont mesurees ici en lisant CE QUI SORT, pas la
source.

AUCUN POSTGRES : le CHAINAGE est sous test. La garde de portee a son harnais.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

IDENTITY = "person_tester"
PROJECT = "proj_EXAMPLE"
ORG = "org_EXAMPLE"
SECRET = "s3cr3t-never-in-an-answer"


def _tool(name: str):
    import core.mcp_profiles as profiles
    import core.project_access_mcp as door

    captured: dict[str, object] = {}

    def _record(_mcp, handler, **_kwargs):
        captured[handler.__name__] = handler
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        door.register(object())
    finally:
        profiles.register_profiled = original
    return captured[name]


def _declaration(name: str) -> dict:
    import core.mcp_profiles as profiles
    import core.project_access_mcp as door

    seen: dict[str, dict] = {}

    def _record(_mcp, handler, **kwargs):
        seen[handler.__name__] = kwargs
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        door.register(object())
    finally:
        profiles.register_profiled = original
    return seen[name]


@pytest.fixture()
def a_reader(monkeypatch):
    import core.db as core_db
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)
    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


@pytest.fixture()
def a_manager(monkeypatch):
    import core.db as core_db
    import core.mcp_scope as mcp_scope
    import core.project_access as project_access

    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *a, **k: SimpleNamespace(allowed=True, org_id=ORG),
    )

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


def _payload(result) -> dict:
    return result.structured_content["data"]


def _refusal(excinfo) -> dict:
    return json.loads(str(excinfo.value))


def _everything(result) -> str:
    """Tout ce que l'appel rend, les deux canaux joints -- le texte ET l'enveloppe."""
    return result.content[0].text + json.dumps(result.structured_content)


# --------------------------------------------------------------------------- #
# 1. La lecture -- et les deux sources d'une capacite tenues a part.
# --------------------------------------------------------------------------- #


def test_the_read_composes_through_the_screens_own_function(a_reader, monkeypatch):
    import core.project_access_surface as surface

    seen: dict = {}

    def _read(project_id, conn, *, actor):
        seen.update({"project_id": project_id, "actor": actor, "conn": conn})
        return {"members": []}

    monkeypatch.setattr(surface, "read_project_access", _read)
    _tool("read_project_access")(project_id=PROJECT)
    assert seen["project_id"] == PROJECT
    assert seen["actor"] == IDENTITY
    assert seen["conn"] is not None


def test_the_two_sources_of_a_capability_stay_apart(a_reader, monkeypatch):
    """Une capacite effective seule ne se repare pas.

    Monter un octroi ne fait rien pour quelqu'un que son role d'organisation
    plafonne deja plus bas ; un lecteur qui ne voit qu'un nombre ne sait pas
    lequel des deux changer.
    """
    import core.project_access_surface as surface

    monkeypatch.setattr(
        surface,
        "read_project_access",
        lambda *a, **k: {
            "members": [
                {
                    "identity": "someone@example.com",
                    "role": "member",
                    "capability": "manage",
                    "effective_capability": "edit",
                }
            ]
        },
    )
    text = _tool("read_project_access")(project_id=PROJECT).content[0].text
    assert "org role member" in text
    assert "project grant manage" in text
    assert "effective edit" in text


def test_an_empty_project_names_where_access_comes_from(a_reader, monkeypatch):
    import core.project_access_surface as surface

    monkeypatch.setattr(surface, "read_project_access", lambda *a, **k: {"members": []})
    text = _tool("read_project_access")(project_id=PROJECT).content[0].text
    assert "organization membership" in text
    assert "nothing here invites anyone" in text
    assert "app." not in text


# --------------------------------------------------------------------------- #
# 2. LA promesse qu'aucune revue ne verra : le secret ne sort pas.
# --------------------------------------------------------------------------- #


def test_the_ceremony_runs_in_one_transaction_in_the_right_order(a_manager, monkeypatch):
    import core.project_access_surface as surface

    order: list[str] = []
    monkeypatch.setattr(
        surface,
        "prepare_grant_change",
        lambda conn, **k: order.append("prepare") or {"change_id": "pac_1"},
    )
    monkeypatch.setattr(
        surface,
        "issue_grant_confirmation",
        lambda conn, **k: order.append("issue")
        or {"confirmation_id": "c_1", "confirmation_secret": SECRET, "expires_at": None},
    )

    def _confirm(conn, *, change_id, actor, confirmation_id, confirmation_secret, project_id):
        order.append("confirm")
        assert confirmation_secret == SECRET, "le secret frappe doit etre celui depense"
        assert change_id == "pac_1"
        return {"change_id": change_id, "state": "confirmed"}

    monkeypatch.setattr(surface, "confirm_grant_change", _confirm)
    _tool("grant_project_access")(
        project_id=PROJECT,
        intent={"action": "set_capability", "identity": "a@example.com", "capability": "edit"},
        idempotency_key="k-1",
    )
    assert order == ["prepare", "issue", "confirm"]


def test_the_confirmation_secret_reaches_neither_channel(a_manager, monkeypatch):
    """Mesure sur CE QUI SORT, pas sur la source.

    Le service rend ici un secret dans son resultat -- ce qu'il ne fait pas
    aujourd'hui. La porte doit le retirer quand meme : le jour ou un service
    changerait d'avis, il ne fuirait pas par cette porte.
    """
    import core.project_access_surface as surface

    monkeypatch.setattr(surface, "prepare_grant_change", lambda conn, **k: {"change_id": "pac_1"})
    monkeypatch.setattr(
        surface,
        "issue_grant_confirmation",
        lambda conn, **k: {"confirmation_id": "c_1", "confirmation_secret": SECRET, "expires_at": None},
    )
    monkeypatch.setattr(
        surface,
        "confirm_grant_change",
        lambda conn, **k: {
            "change_id": "pac_1",
            "state": "confirmed",
            "confirmation_secret": SECRET,
            "token": "t-1",
            "url": "https://example.com/resume",
        },
    )
    result = _tool("grant_project_access")(
        project_id=PROJECT,
        intent={"action": "set_capability", "identity": "a@example.com", "capability": "edit"},
        idempotency_key="k-1",
    )
    everything = _everything(result)
    assert SECRET not in everything
    assert "t-1" not in everything
    assert "https://example.com/resume" not in everything
    assert _payload(result)["state"] == "confirmed"


def test_a_handoff_returns_its_object_and_never_its_link(a_manager, monkeypatch):
    """L'objet est operable par un agent, le lien secret ne l'est pas."""
    import core.project_access_surface as surface

    monkeypatch.setattr(
        surface,
        "prepare_access_handoff",
        lambda conn, **k: {
            "handoff_id": "paccess_1",
            "state": "pending",
            "expires_at": "2026-09-07T00:00:00+00:00",
            "resume_ref": k["resume_ref"],
            "url": "https://example.com/handoff/secret",
        },
    )
    result = _tool("grant_project_access")(
        project_id=PROJECT,
        intent={"action": "hand_off", "resume_ref": "/org/x/project/y"},
        idempotency_key="k-2",
    )
    everything = _everything(result)
    assert "paccess_1" in everything
    assert "/org/x/project/y" in everything
    assert "https://example.com/handoff/secret" not in everything


def test_the_capability_vocabulary_is_the_services_own_and_is_not_copied():
    """Y compris `null`, qui est comment un octroi se RETIRE.

    Une porte qui ne saurait pas l'exprimer pourrait elever un acces et jamais
    en abaisser un.
    """
    import core.project_access_mcp as door
    import core.project_access_surface as surface

    assert set(door._capabilities()) == set(surface._CAPABILITIES)
    assert None in door._capabilities()


def test_removing_a_grant_is_expressible_and_reaches_the_service(a_manager, monkeypatch):
    import core.project_access_surface as surface

    seen: dict = {}
    monkeypatch.setattr(
        surface,
        "prepare_grant_change",
        lambda conn, **k: seen.update(k) or {"change_id": "pac_1"},
    )
    monkeypatch.setattr(
        surface,
        "issue_grant_confirmation",
        lambda conn, **k: {"confirmation_id": "c_1", "confirmation_secret": SECRET},
    )
    monkeypatch.setattr(surface, "confirm_grant_change", lambda conn, **k: {"state": "confirmed"})
    _tool("grant_project_access")(
        project_id=PROJECT,
        intent={"action": "set_capability", "identity": "a@example.com", "capability": None},
        idempotency_key="k-3",
    )
    assert seen["after_capability"] is None
    assert seen["idempotency_key"] == "k-3"


# --------------------------------------------------------------------------- #
# 3. Les refus de forme et les rangs declares.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kwargs", "code"),
    [
        ("read_project_access", {"project_id": ""}, "missing_param"),
        ("grant_project_access", {"project_id": "", "intent": {"action": "hand_off"}, "idempotency_key": "k"}, "missing_param"),
        ("grant_project_access", {"project_id": PROJECT, "intent": {}, "idempotency_key": "k"}, "missing_param"),
        ("grant_project_access", {"project_id": PROJECT, "intent": {"action": "revoke_all"}, "idempotency_key": "k"}, "unknown_action"),
        # Sans cle de reprise : un octroi rejoue ouvrirait un second changement.
        ("grant_project_access", {"project_id": PROJECT, "intent": {"action": "hand_off", "resume_ref": "/org/x"}, "idempotency_key": ""}, "missing_idempotency_key"),
        ("grant_project_access", {"project_id": PROJECT, "intent": {"action": "set_capability", "capability": "edit"}, "idempotency_key": "k"}, "missing_param"),
        # `capability` absente n'est PAS `capability: null` : l'une est un oubli,
        # l'autre est un retrait voulu, et la porte ne les confond pas.
        ("grant_project_access", {"project_id": PROJECT, "intent": {"action": "set_capability", "identity": "a@example.com"}, "idempotency_key": "k"}, "missing_param"),
        ("grant_project_access", {"project_id": PROJECT, "intent": {"action": "set_capability", "identity": "a@example.com", "capability": "owner"}, "idempotency_key": "k"}, "invalid_param"),
        ("grant_project_access", {"project_id": PROJECT, "intent": {"action": "hand_off"}, "idempotency_key": "k"}, "missing_param"),
    ],
)
def test_the_door_refuses_the_argument_before_it_opens_anything(monkeypatch, name, kwargs, code):
    import core.db as core_db
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)

    def _never(_identity):  # pragma: no cover -- il doit ne jamais etre appele
        raise AssertionError("no connection may be opened for a malformed call")

    monkeypatch.setattr(core_db, "request_connection", _never)
    with pytest.raises(ToolError) as excinfo:
        _tool(name)(**kwargs)
    assert _refusal(excinfo)["code"] == code


def test_the_two_declarations_are_the_ranks_the_document_states():
    read = _declaration("read_project_access")
    assert (read["profile"], read["effect"], read["confirmation_mode"]) == (
        "insights",
        "read",
        "none",
    )
    assert read["data_class"] == "sensitive", "une liste d'identites n'est pas operationnelle"
    write = _declaration("grant_project_access")
    assert (write["profile"], write["effect"], write["confirmation_mode"]) == (
        "governance",
        "confirmed_write",
        "human",
    )
    assert write["data_class"] == "sensitive"
