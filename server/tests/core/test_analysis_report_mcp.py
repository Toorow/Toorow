"""La porte MCP des Reports gouvernés — story 67.23.

CE QUE CES TESTS TIENNENT, ET POURQUOI CHACUN EXISTE. La porte est mince : trois
verbes qui délèguent à `analyze_artifacts`. Ce qui peut mal tourner n'est donc
pas le calcul — c'est **ce qui entoure la délégation**, et c'est exactement ce
qu'aucun test du service ne peut voir puisqu'il reçoit déjà l'organisation, le
projet et l'acteur en arguments :

  * la portée est prouvée AVANT la lecture, et au bon rang — `edit` sur celui qui
    exécute, parce qu'il crée un Result et dépense le budget d'entrepôt ;
  * la connexion qui écrit est ARMÉE, donc le second plancher s'applique. Une
    connexion nue ici serait le défaut que `db.py:313-316` mesure à 113 modules
    sur 118 ;
  * un Report qui n'est pas dans ce Projet reçoit le refus d'un Report qui
    n'existe pas — comparer deux refus n'apprend pas qu'un objet existe ;
  * la délégation passe les identités qu'elle a LUES, jamais celles qu'on lui a
    dites : l'organisation vient du projet, pas de l'appelant.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _armed_conn(row=("org_t",), columns=("org_id",)):
    """Une connexion doublée, rendue par `request_connection`."""
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.description = [(c,) for c in columns]
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cursor)
    cm.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cm)
    return conn


def _tools(monkeypatch, *, identity="alice@example.com"):
    """Les corps des trois outils, attrapés à l'enregistrement.

    `register_profiled` est importé DANS `register()` — le seam anti-cycle de
    tout `server/core` — donc c'est `core.mcp_profiles` qu'il faut doubler.
    """
    from core import analysis_report_mcp, mcp_profiles

    captured: dict = {}
    monkeypatch.setattr(
        mcp_profiles,
        "register_profiled",
        lambda mcp, fn, **kw: captured.setdefault(fn.__name__, fn),
    )
    monkeypatch.setattr(analysis_report_mcp, "_identity", lambda: identity)
    analysis_report_mcp.register(MagicMock())
    assert {"list_reports", "get_report_versions", "run_report_version"} <= set(captured)
    return captured


# ---------------------------------------------------------------------------
# La portée, et le rang qu'elle exige
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "call", "capability"),
    [
        ("list_reports", lambda f: f(project_id="proj_t"), None),
        (
            "get_report_versions",
            lambda f: f(project_id="proj_t", report_id="rpt_1"),
            None,
        ),
        (
            "run_report_version",
            lambda f: f(project_id="proj_t", report_version_id="rptv_1"),
            "edit",
        ),
    ],
)
def test_every_tool_proves_the_scope_and_the_write_demands_edit(
    monkeypatch, tool, call, capability
):
    """Une lecture demande le rang par défaut ; celui qui EXÉCUTE demande `edit`.

    Le rang n'est pas une préférence : `run_report_version` crée un Result et
    dépense le budget d'entrepôt du projet. Une exécution croisée ne fuit pas une
    donnée, elle en FABRIQUE une.
    """
    from core import analysis_report_mcp

    guard = MagicMock()
    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", guard)
    monkeypatch.setattr(
        "core.main._resolve_project", lambda pid, identity=None: pid, raising=False
    )
    with (
        patch("core.db.request_connection", return_value=_armed_conn()),
        patch("core.analyze_artifacts.list_reports", return_value=[]),
        patch("core.analyze_artifacts.get_report", return_value={}),
        patch("core.analyze_artifacts.run_report_version", return_value={"run_id": "r"}),
        patch("core.audit.write_audit_row"),
    ):
        call(_tools(monkeypatch)[tool])

    guard.assert_called_once()
    assert guard.call_args[0][0] == "proj_t"
    assert guard.call_args.kwargs.get("minimum_capability") == capability


def test_the_connection_that_writes_is_ARMED(monkeypatch):
    """Le second plancher. Une connexion nue ne le porte pas, et c'est celle qui
    ÉCRIT qui doit l'avoir -- armer une autre n'achète rien."""
    from core import analysis_report_mcp

    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", MagicMock())
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    with (
        patch("core.db.request_connection", return_value=_armed_conn()) as armed,
        patch("core.db.get_connection") as bare,
        patch("core.analyze_artifacts.run_report_version", return_value={"run_id": "r"}),
        patch("core.audit.write_audit_row"),
    ):
        _tools(monkeypatch)["run_report_version"](
            project_id="proj_t", report_version_id="rptv_1"
        )

    armed.assert_called_once_with("alice@example.com")
    bare.assert_not_called()


# ---------------------------------------------------------------------------
# Ce que la délégation transmet
# ---------------------------------------------------------------------------


def test_the_org_is_READ_from_the_project_and_never_received(monkeypatch):
    """Recevoir l'organisation en argument laisserait un appelant la choisir."""
    from core import analysis_report_mcp

    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", MagicMock())
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    with (
        patch("core.db.request_connection", return_value=_armed_conn(("org_from_db",))),
        patch("core.analyze_artifacts.list_reports", return_value=[]) as listed,
    ):
        _tools(monkeypatch)["list_reports"](project_id="proj_t")

    assert listed.call_args.kwargs["org_id"] == "org_from_db"
    assert listed.call_args.kwargs["project_id"] == "proj_t"


def test_a_project_that_resolves_to_no_org_refuses_as_absent(monkeypatch):
    from core import analysis_report_mcp
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", MagicMock())
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    with (
        patch("core.db.request_connection", return_value=_armed_conn(None, ())),
        pytest.raises(ToolError) as exc,
    ):
        _tools(monkeypatch)["list_reports"](project_id="proj_t")

    assert json.loads(exc.value.args[0])["code"] == "not_found"


def test_a_report_of_another_project_refuses_like_one_that_does_not_exist(monkeypatch):
    """Distinguer « pas à vous » de « n'existe pas » apprend qu'il existe."""
    from core import analysis_report_mcp
    from core.analyze_artifacts import ArtifactNotFound
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", MagicMock())
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    with (
        patch("core.db.request_connection", return_value=_armed_conn()),
        patch(
            "core.analyze_artifacts.get_report",
            side_effect=ArtifactNotFound("report not found in this Project"),
        ),
        pytest.raises(ToolError) as exc,
    ):
        _tools(monkeypatch)["get_report_versions"](project_id="proj_t", report_id="rpt_x")

    body = json.loads(exc.value.args[0])
    assert body["code"] == "not_found"


# ---------------------------------------------------------------------------
# Ce que la liste dit d'elle-même
# ---------------------------------------------------------------------------


def test_an_empty_list_says_the_read_HAPPENED(monkeypatch):
    """Une liste vide et une lecture qui n'a pas eu lieu ne se rendent jamais
    l'une pour l'autre. Le compte est ce qui les sépare."""
    from core import analysis_report_mcp

    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", MagicMock())
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    with (
        patch("core.db.request_connection", return_value=_armed_conn()),
        patch("core.analyze_artifacts.list_reports", return_value=[]),
    ):
        answer = _tools(monkeypatch)["list_reports"](project_id="proj_t")

    assert answer["count"] == 0
    assert answer["reports"] == []
    assert answer["include_archived"] is False


def test_the_archived_ones_stay_REACHABLE_by_asking(monkeypatch):
    """Un archivage qui ne vide pas la liste est un drapeau, pas une retraite --
    et un archivage qui rend l'objet introuvable détruit une preuve."""
    from core import analysis_report_mcp

    monkeypatch.setattr(analysis_report_mcp, "refuse_unless_project_scope", MagicMock())
    monkeypatch.setattr("core.main._resolve_project", lambda pid, identity=None: pid, raising=False)

    with (
        patch("core.db.request_connection", return_value=_armed_conn()),
        patch("core.analyze_artifacts.list_reports", return_value=[]) as listed,
    ):
        _tools(monkeypatch)["list_reports"](project_id="proj_t", include_archived=True)

    assert listed.call_args.kwargs["include_archived"] is True


# ---------------------------------------------------------------------------
# Les noms, et le piège qu'ils évitent
# ---------------------------------------------------------------------------


def test_the_governed_tools_never_take_the_name_of_the_connector_one():
    """`get_report` sert le rapport nommé d'un CONNECTEUR -- un autre objet.

    `glossary.md:492` : « Report » vaut pour les deux, jamais sans qualifier.
    Deux outils du même nom sur le fil, c'est un modèle qui choisit au hasard.
    """
    import asyncio

    from core import main as core_main

    names = {t.name for t in asyncio.run(core_main.mcp._list_tools())}
    assert {"list_reports", "get_report_versions", "run_report_version"} <= names
    # Et celui du connecteur est toujours là, sous son nom, intact.
    assert "get_report" in names
