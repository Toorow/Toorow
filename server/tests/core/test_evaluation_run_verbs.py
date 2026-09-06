# -*- coding: utf-8 -*-
"""Les trois verbes de la Regression Run -- la porte MCP du jugement.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST CELA. `evaluation_mcp.py` a dit
pendant dix-neuf jours, dans sa propre docstring, que declencher une evaluation
depuis MCP n'existait pas. L'audit de gap du 2026-09-05 a mesure ce que cela
coutait : `run a pinned cohort and compare it to an approved baseline -- READS
ONLY. Opening, executing, baselining and gating a run are console-only.`

Les verbes sont batis sur UNE promesse : **ils appellent les MEMES fonctions que
l'ecran** (`core.evaluation_runs`, `core.evaluation_run_executor`). Une promesse
pareille ne se lit pas dans le code : elle se verifie en remplacant le service
par une doublure et en exigeant que l'outil l'ait appele, avec les arguments que
la console lui passe.

TROIS INVARIANTS QUE LE DOCUMENT NOMME, ET QU'UNE DOUBLURE PEUT MESURER :
  1. la version du catalogue d'outils est resolue **cote serveur**, jamais recue
     de l'appelant -- une execution dont la version aurait ete devinee n'est
     comparable a aucune autre, et la porte REFUSE quand le catalogue manque ;
  2. **finaliser ne cree, ne deplace et ne met a jour aucune base** -- une base
     ne s'approuve que par un acte explicite ;
  3. une decision de porte est une **preuve pour le proprietaire, jamais une
     transition**.

AUCUN POSTGRES. Ce qui est sous test n'est pas l'ecriture -- elle a ses propres
tests, pg-gated -- mais le CHAINAGE : garde, puis service partage, puis
enveloppe AD-1. La garde de portee a son harnais a elle
(`tests/isolation/test_mcp_tool_scope_refusal.py`, ou les trois verbes entrent le
jour ou ils sont ecrits) et n'est pas redite ici.
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
CATALOG = "cat_2026_09_05"

RUN_PINS = {
    "run_profile_id": "erp_1",
    "context_version_set_id": "cvs_1",
    "semantic_view_id": "sv_1",
    "semantic_view_version_id": "svv_1",
    "model_ref": "claude-opus-5",
    "as_of": "2026-09-05",
}


def _tool(name: str):
    """Le handler que `evaluation_mcp` enregistre, capte a l'enregistrement."""
    import core.evaluation_mcp as door
    import core.mcp_profiles as profiles

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
    import core.evaluation_mcp as door
    import core.mcp_profiles as profiles

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
def a_writer(monkeypatch):
    """Une identite qui peut ecrire dans le projet, et une connexion qui s'ouvre.

    Les trois verbes passent par `resolve_strict_resource_access`, la couture de
    `governance_mcp`, et non par le sceau de portee. C'est cette resolution-la qui
    est neutralisee ici ; son refus est prouve dans le harnais d'isolation.
    """
    import core.db as core_db
    import core.evaluation_mcp as door
    import core.mcp_scope as mcp_scope
    import core.project_access as project_access

    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *a, **k: SimpleNamespace(allowed=True, org_id=ORG),
    )
    monkeypatch.setattr(door, "_live_tool_catalog", lambda: CATALOG)

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


def _payload(result) -> dict:
    return result.structured_content["data"]


def _refusal(excinfo) -> dict:
    return json.loads(str(excinfo.value))


# --------------------------------------------------------------------------- #
# 1. Ouvrir -- les pins de la console, et la version du catalogue cote serveur.
# --------------------------------------------------------------------------- #


def test_opening_passes_the_screens_own_pins_to_the_screens_own_function(a_writer, monkeypatch):
    import core.evaluation_runs as service

    seen: dict = {}

    def _open(conn, **kwargs):
        seen.update(kwargs)
        return {"id": "er_1", "lifecycle": "recording", "evidence_mode": "offline", "unresolved_pins": []}

    monkeypatch.setattr(service, "open_evaluation_run", _open)
    result = _tool("open_evaluation_run")(project_id=PROJECT, run=dict(RUN_PINS))

    assert seen["org_id"] == ORG
    assert seen["project_id"] == PROJECT
    assert seen["run_profile_id"] == "erp_1"
    assert seen["context_version_set_id"] == "cvs_1"
    assert seen["semantic_view_version_id"] == "svv_1"
    assert seen["actor"] == IDENTITY
    assert _payload(result)["run"]["id"] == "er_1"


def test_the_tool_catalog_version_is_never_taken_from_the_caller(a_writer, monkeypatch):
    """L'invariant 1 : une version devinee rend l'execution comparable a rien.

    L'appelant en propose une ; la porte pose celle du catalogue vivant.
    """
    import core.evaluation_runs as service

    seen: dict = {}
    monkeypatch.setattr(
        service,
        "open_evaluation_run",
        lambda conn, **k: seen.update(k) or {"id": "er_1", "unresolved_pins": []},
    )
    _tool("open_evaluation_run")(
        project_id=PROJECT, run={**RUN_PINS, "tool_catalog_version": "whatever-the-caller-said"}
    )
    assert seen["tool_catalog_version"] == CATALOG


def test_a_run_is_refused_when_the_live_catalog_is_unavailable(a_writer, monkeypatch):
    """Refusee, pas ouverte avec un pin invente -- le refus meme que la console rend."""
    import core.evaluation_mcp as door
    import core.evaluation_runs as service

    def _boom():
        raise door._tool_error("tool_catalog_unavailable", "The live tool catalog is not available.")

    monkeypatch.setattr(door, "_live_tool_catalog", _boom)
    monkeypatch.setattr(
        service,
        "open_evaluation_run",
        lambda *a, **k: pytest.fail("a run must not be opened without a catalog version"),
    )
    with pytest.raises(ToolError) as excinfo:
        _tool("open_evaluation_run")(project_id=PROJECT, run=dict(RUN_PINS))
    assert _refusal(excinfo)["code"] == "tool_catalog_unavailable"


def test_named_cases_are_pinned_in_the_same_act(a_writer, monkeypatch):
    import core.evaluation_runs as service

    pinned: list[dict] = []
    monkeypatch.setattr(
        service, "open_evaluation_run", lambda conn, **k: {"id": "er_1", "unresolved_pins": []}
    )
    monkeypatch.setattr(
        service,
        "add_run_case",
        lambda conn, **k: pinned.append(k) or {"id": "erc_%d" % len(pinned)},
    )
    result = _tool("open_evaluation_run")(
        project_id=PROJECT,
        run=dict(RUN_PINS),
        cases=[{"golden_question_version_id": "gqv_1", "result_id": "qr_1"}],
    )
    assert len(pinned) == 1
    assert pinned[0]["run_id"] == "er_1"
    assert pinned[0]["golden_question_version_id"] == "gqv_1"
    assert pinned[0]["result_id"] == "qr_1"
    assert len(_payload(result)["cases"]) == 1


# --------------------------------------------------------------------------- #
# 2. Avancer -- executer par l'executeur, finaliser sans toucher a une base.
# --------------------------------------------------------------------------- #


def test_execute_goes_through_the_executor_the_console_calls(a_writer, monkeypatch):
    import core.evaluation_run_executor as executor

    seen: dict = {}

    def _execute(conn, **kwargs):
        seen.update(kwargs)
        return {"lifecycle": "finalized", "question_count": 3, "verdict_counts": {"sql": {"pass": 3}}}

    monkeypatch.setattr(executor, "execute_evaluation_run", _execute)
    result = _tool("advance_evaluation_run")(
        project_id=PROJECT, run_id="er_1", action="execute", subjects={"gqv_1": {"result_id": "qr_1"}}
    )
    assert seen["run_id"] == "er_1"
    assert seen["actor"] == IDENTITY
    assert seen["subjects"] == {"gqv_1": {"result_id": "qr_1"}}
    assert _payload(result)["question_count"] == 3


def test_finalizing_touches_no_baseline_and_says_so(a_writer, monkeypatch):
    """L'invariant 2, mesure : le module de base n'est meme pas appele.

    Une base ne se met jamais a jour toute seule. Le dire dans la reponse ET ne
    pas appeler la fonction sont deux choses ; ce test exige les deux.
    """
    import core.evaluation_runs as service

    monkeypatch.setattr(
        service,
        "finalize_evaluation_run",
        lambda conn, **k: {"id": k["run_id"], "lifecycle": "finalized", "verdict_counts": {}},
    )
    monkeypatch.setattr(
        service,
        "approve_baseline",
        lambda *a, **k: pytest.fail("finalizing must not approve a baseline"),
    )
    text = _tool("advance_evaluation_run")(
        project_id=PROJECT, run_id="er_1", action="finalize"
    ).content[0].text
    assert "No baseline was created or moved" in text


# --------------------------------------------------------------------------- #
# 3. Decider -- la base, ou la preuve pour le proprietaire.
# --------------------------------------------------------------------------- #


def test_approving_a_baseline_carries_who_and_why(a_writer, monkeypatch):
    import core.evaluation_runs as service

    seen: dict = {}
    monkeypatch.setattr(
        service, "approve_baseline", lambda conn, **k: seen.update(k) or {"id": "eb_1", **k}
    )
    result = _tool("decide_evaluation_run")(
        project_id=PROJECT,
        action="approve_baseline",
        decision={"run_id": "er_1", "approved_by": "owner@example.com", "approval_reason": "green"},
    )
    assert seen["approved_by"] == "owner@example.com"
    assert seen["approval_reason"] == "green"
    assert "superseded, never rewritten" in result.content[0].text


@pytest.mark.parametrize("missing", ["run_id", "approved_by", "approval_reason"])
def test_a_baseline_without_who_or_why_is_refused_before_anything_opens(a_writer, monkeypatch, missing):
    import core.evaluation_runs as service

    monkeypatch.setattr(
        service, "approve_baseline", lambda *a, **k: pytest.fail("must refuse before writing")
    )
    decision = {"run_id": "er_1", "approved_by": "o@example.com", "approval_reason": "green"}
    decision.pop(missing)
    with pytest.raises(ToolError) as excinfo:
        _tool("decide_evaluation_run")(
            project_id=PROJECT, action="approve_baseline", decision=decision
        )
    refusal = _refusal(excinfo)
    assert refusal["code"] == "missing_param"
    assert missing in refusal["message"]


def test_a_gate_decision_says_it_transitions_nothing(a_writer, monkeypatch):
    """L'invariant 3 : une preuve que le proprietaire lit, jamais un passage."""
    import core.evaluation_runs as service

    monkeypatch.setattr(
        service, "emit_gate_decision", lambda conn, **k: {"id": "egd_1", **k}
    )
    text = _tool("decide_evaluation_run")(
        project_id=PROJECT,
        action="emit_gate_decision",
        decision={
            "comparison_id": "ec_1",
            "candidate_owner_workspace": "governance",
            "candidate_object_type": "semantic-view",
            "candidate_object_id": "sv_1",
            "candidate_version_id": "svv_2",
        },
    ).content[0].text
    assert "transitions nothing" in text


# --------------------------------------------------------------------------- #
# 4. Les refus de forme et les rangs declares.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kwargs", "code"),
    [
        ("open_evaluation_run", {"project_id": "", "run": RUN_PINS}, "missing_param"),
        ("open_evaluation_run", {"project_id": PROJECT, "run": {}}, "missing_param"),
        # Un profil absent : une execution n'est comparable qu'a celles du meme.
        ("open_evaluation_run", {"project_id": PROJECT, "run": {"context_version_set_id": "cvs_1"}}, "missing_param"),
        # Un jeu de versions de contexte absent : le contexte se gele, jamais `latest`.
        ("open_evaluation_run", {"project_id": PROJECT, "run": {"run_profile_id": "erp_1"}}, "missing_param"),
        ("advance_evaluation_run", {"project_id": PROJECT, "run_id": "", "action": "execute"}, "missing_param"),
        ("advance_evaluation_run", {"project_id": PROJECT, "run_id": "er_1", "action": "publish"}, "unknown_action"),
        ("decide_evaluation_run", {"project_id": PROJECT, "action": "retire", "decision": {"a": 1}}, "unknown_action"),
        ("decide_evaluation_run", {"project_id": PROJECT, "action": "emit_gate_decision", "decision": {}}, "missing_param"),
    ],
)
def test_the_verbs_refuse_the_argument_before_they_open_anything(monkeypatch, name, kwargs, code):
    import core.db as core_db
    import core.evaluation_mcp as door
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(door, "_live_tool_catalog", lambda: CATALOG)

    def _never(_identity):  # pragma: no cover -- il doit ne jamais etre appele
        raise AssertionError("no connection may be opened for a malformed call")

    monkeypatch.setattr(core_db, "request_connection", _never)
    with pytest.raises(ToolError) as excinfo:
        _tool(name)(**kwargs)
    assert _refusal(excinfo)["code"] == code


def test_the_three_verbs_carry_the_ranks_the_console_guards_them_at():
    """Une ecriture qui glisserait en `insights` entrerait dans le catalogue par
    defaut de tout hote et s'executerait sans ceremonie."""
    for name in ("open_evaluation_run", "advance_evaluation_run"):
        d = _declaration(name)
        assert (d["profile"], d["effect"], d["confirmation_mode"]) == (
            "operations",
            "confirmed_write",
            "human",
        ), name
    decide = _declaration("decide_evaluation_run")
    assert (decide["profile"], decide["effect"], decide["confirmation_mode"]) == (
        "governance",
        "confirmed_write",
        "human",
    )
    assert decide["data_class"] == "sensitive"


def test_the_read_still_reads_and_now_serves_the_profiles_a_run_must_pin():
    """Le parametre ajoute a la LECTURE reste une lecture.

    `mcp-tool-surface.md` : une ecriture ne voyage jamais sur le parametre d'une
    lecture. Celui-ci sert les profils et leurs bases -- rien d'autre.
    """
    read = _declaration("get_evaluation_runs")
    assert (read["profile"], read["effect"], read["confirmation_mode"]) == (
        "insights",
        "read",
        "none",
    )
    import inspect

    import core.evaluation_mcp as door

    assert "run_profiles" in inspect.signature(door.get_evaluation_runs).parameters
