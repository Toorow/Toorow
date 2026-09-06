"""48.2 tâche 7 -- la parité REST/MCP du hash de contenu, prouvée sur la structure.

MESURE DU 2026-08-04, qui explique pourquoi la tâche a stagné :
``country_workspace_commands.run_country_workspace_command`` avait EXACTEMENT UN
appelant -- la route REST -- et aucun outil MCP master-data n'existait
(``governance_mcp`` ne couvre que les changements d'agent par datastream). La
ligne de tracker disait « désormais possible et non faite » : c'était possible,
et c'était une porte à bâtir, pas un test à écrire.

CE QUE CES TESTS TIENNENT, ET POURQUOI CE N'EST PAS UN TEST D'ÉGALITÉ.
Comparer deux hashes calculés par deux chemins serait satisfait le jour où les
deux chemins se mettraient d'accord sur une valeur fausse. Ce qui rend la parité
vraie n'est pas une assertion, c'est qu'il n'existe qu'UN calculateur : les deux
portes appellent la même fonction. Ces tests épinglent donc la STRUCTURE --
qu'aucune des deux ne recalcule quoi que ce soit -- puis vérifient sur un double
que les deux transmettent les mêmes arguments.

Et ils tiennent la seconde chose qu'une seconde porte peut casser : sa garde. Une
porte MCP plus permissive que la porte REST n'est pas une seconde porte, c'est un
contournement de la première.

Tout OFFLINE.
"""

from __future__ import annotations

import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import master_data_mcp as md  # noqa: E402

_CORE = pathlib.Path(__file__).resolve().parents[2] / "core"


def _source(name: str) -> str:
    return (_CORE / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# La structure : un seul calculateur, deux portes
# ---------------------------------------------------------------------------


def test_neither_door_computes_a_content_hash_of_its_own():
    """Le coeur de la parité. Si une porte se met à hacher elle-même, les deux
    peuvent rester vertes séparément et diverger en production."""
    for name in ("master_data_mcp.py", "governance_surface_api.py"):
        text = _source(name)
        assert "content_hash(" not in text, f"{name} calcule un hash au lieu de deleguer"


def test_both_doors_call_the_same_runner():
    for name in ("master_data_mcp.py", "governance_surface_api.py"):
        text = _source(name)
        assert "run_country_workspace_command" in text
        assert "prepare_country_publish_confirmation" in text


# ---------------------------------------------------------------------------
# Les arguments : même projet, même org, même clé, même action
# ---------------------------------------------------------------------------


def _run_mcp(action: str, payload: dict | None = None):
    seen: dict = {}

    def _fake_run(conn, **kw):
        seen.update(kw)
        return {"version": {"content_hash": "c" * 64}}

    decision = MagicMock(allowed=True, org_id="org_EXAMPLE", reason=None)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)

    with patch.object(md, "_identity", return_value="operator@example.com"), patch(
        "core.db.get_connection", return_value=ctx
    ), patch("core.db.set_local_access_context"), patch(
        "core.project_access.resolve_strict_resource_access", return_value=decision
    ), patch("core.country_workspace_commands.run_country_workspace_command", _fake_run), patch(
        "core.country_workspace_commands.prepare_country_publish_confirmation", _fake_run
    ):
        result = md._run("proj_EXAMPLE", action, payload or {}, "idem-1")
    return result, seen


def test_the_mcp_door_hands_the_runner_exactly_what_the_route_hands_it():
    _result, seen = _run_mcp("save_hierarchy", {"nodes": []})
    assert seen["project_id"] == "proj_EXAMPLE"
    # L'org vient de la DÉCISION d'accès, jamais du client : un org_id fourni par
    # l'appelant serait une écriture inter-tenant offerte.
    assert seen["org_id"] == "org_EXAMPLE"
    assert seen["actor"] == "operator@example.com"
    assert seen["idempotency_key"] == "idem-1"
    assert seen["action"] == "save_hierarchy"


def test_the_hash_the_tool_reports_is_the_runner_s_own():
    result, _seen = _run_mcp("save_hierarchy")
    assert result["version"]["content_hash"] == "c" * 64


# ---------------------------------------------------------------------------
# La garde : la seconde porte ne peut pas être plus permissive
# ---------------------------------------------------------------------------


def test_the_mcp_door_uses_the_same_capability_mapping_as_the_route():
    """`manage` pour publier, `edit` pour éditer -- la même bascule des deux côtés.

    Une porte MCP qui accepterait `edit` pour publier serait une élévation de
    privilège atteignable par un agent.
    """
    assert md._minimum_capability("publish") == "manage"
    assert md._minimum_capability("prepare_publish") == "manage"
    assert md._minimum_capability("save_hierarchy") == "edit"
    assert md._minimum_capability("apply_preset") == "edit"


def test_an_unauthorized_caller_gets_not_found_never_forbidden():
    decision = MagicMock(allowed=False, org_id=None, reason="insufficient_capability")
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    with patch.object(md, "_identity", return_value="stranger@example.com"), patch(
        "core.db.get_connection", return_value=ctx
    ), patch("core.db.set_local_access_context"), patch(
        "core.project_access.resolve_strict_resource_access", return_value=decision
    ), patch("core.country_workspace_commands.run_country_workspace_command") as runner:
        with pytest.raises(Exception, match="not_found"):
            md._run("proj_OTHER", "save_hierarchy", {}, "idem-1")
    runner.assert_not_called()


def test_an_anonymous_caller_never_reaches_the_database():
    with patch.object(md, "_identity", return_value="anonymous"), patch(
        "core.db.get_connection"
    ) as conn:
        with pytest.raises(Exception, match="not_found"):
            md._run("proj_EXAMPLE", "save_hierarchy", {}, "idem-1")
    conn.assert_not_called()


def test_a_missing_idempotency_key_is_refused_before_anything_opens():
    with patch.object(md, "_identity", return_value="operator@example.com"), patch(
        "core.db.get_connection"
    ) as conn:
        with pytest.raises(Exception, match="missing_idempotency_key"):
            md._run("proj_EXAMPLE", "save_hierarchy", {}, "   ")
    conn.assert_not_called()


def test_the_access_context_is_set_before_the_decision_is_taken():
    """Décider l'accès avant d'armer la RLS lirait sous un contexte plus large
    que celui qui servira ensuite.

    Story 21.6 : le contexte ne s'arme plus à la main ici, il arrive AVEC la
    connexion (`core.db.request_connection`). L'invariant est le même et il est
    même plus fort — l'acquisition précède forcément la décision — mais il se
    lit désormais sur l'acquisition, pas sur un appel qu'on pouvait oublier."""
    text = _source("master_data_mcp.py")
    assert "set_local_access_context(" not in text, (
        "le module réarme la RLS à la main : le contexte appartient à l'acquisition"
    )
    assert text.index("with request_connection(") < text.index("resolve_strict_resource_access(")


# ---------------------------------------------------------------------------
# Le catalogue : ce que les trois outils DÉCLARENT
# ---------------------------------------------------------------------------


def test_the_three_tools_declare_what_they_really_do():
    """Un seul outil pour les trois actes aurait dû déclarer un seul
    `confirmation_mode` : `human` sur une sauvegarde de brouillon serait un
    mensonge dans le catalogue, `none` sur une publication un pire."""
    import core.main  # noqa: F401  -- l'enregistrement se fait au boot
    from core.mcp_profiles import registered_declarations

    decls = {d.name: d for d in registered_declarations() if "country_master_data" in d.name}
    assert set(decls) == {
        "edit_country_master_data",
        "prepare_country_master_data_publish",
        "publish_country_master_data",
    }
    assert decls["prepare_country_master_data_publish"].effect == "prepare"
    assert decls["publish_country_master_data"].confirmation_mode == "human"
    assert decls["edit_country_master_data"].effect == "confirmed_write"


@pytest.mark.parametrize("action", ["publish", "prepare_publish"])
def test_the_edit_tool_refuses_to_publish(action):
    """Sans cette garde, la bascule `manage`/`edit` serait contournable en passant
    `publish` à l'outil déclaré pour les brouillons -- une élévation qu'un agent
    atteint tout seul."""
    with pytest.raises(Exception, match="wrong_tool"):
        md.refuse_manage_action(action)


@pytest.mark.parametrize("action", ["save_hierarchy", "apply_preset", "start_draft"])
def test_the_edit_tool_lets_the_draft_actions_through(action):
    """La garde ne doit pas devenir un refus général : elle ne connaît que les
    deux actes qui exigent `manage`."""
    md.refuse_manage_action(action)
