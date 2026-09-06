# -*- coding: utf-8 -*-
"""La porte MCP de RELECTURE du Test -- relire une reaction, et y repondre.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST CELA. C'etait la derniere des trois
lignes du Test que l'audit de gap du 2026-09-05 a mesurees console-seulement :
*« re-read an explicit reaction in its exact context of result, render and trace
-- `submit_feedback` WRITES one. No tool reads a feedback back, classifies it or
resolves it. »* Un modele pouvait laisser une reaction sur une figure qu'il avait
produite et n'en jamais relire une.

La porte est batie sur UNE promesse : **elle appelle les MEMES fonctions que
l'ecran** (`core.feedback_review`). Et sur une seconde, qui est celle qui pourrit
le plus vite : **les vocabulaires fermes sont LUS dans le service, jamais
recopies ici**. Une copie vieillit, et le jour ou elle vieillit la porte accepte
un etat que l'ecran refuse. Le test qui mesure cela n'ecrit aucune liste : il
demande au service la sienne et exige que la porte la serve.

AUCUN POSTGRES. Ce qui est sous test est le CHAINAGE : garde, puis service
partage, puis enveloppe AD-1. La garde de portee a son harnais a elle
(`tests/isolation/test_mcp_tool_scope_refusal.py`, ou les deux outils entrent le
jour ou ils sont ecrits).
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

A_REVIEW = {
    "state": "triaged",
    "affected_dimension": "semantic_correctness",
    "human_verdict": "fail",
    "severity": "major",
    "reason": "the total was not allowed to be a total",
    "retry_key": "rk-1",
}


def _tool(name: str):
    import core.feedback_review_mcp as door
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
    import core.feedback_review_mcp as door
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
def a_reader(monkeypatch):
    import core.db as core_db
    import core.feedback_review_mcp as door
    import core.mcp_scope as mcp_scope

    monkeypatch.setattr(mcp_scope, "refuse_unless_project_scope", lambda *a, **k: None)
    monkeypatch.setattr(mcp_scope, "caller_identity", lambda: IDENTITY)
    monkeypatch.setattr(door, "_org_of", lambda _conn, _project: ORG)

    @contextmanager
    def _open(_identity):
        yield MagicMock()

    monkeypatch.setattr(core_db, "request_connection", _open)
    return IDENTITY


@pytest.fixture()
def a_writer(monkeypatch):
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


# --------------------------------------------------------------------------- #
# 1. Les quatre lentilles -- quatre questions, quatre fonctions du service.
# --------------------------------------------------------------------------- #


def test_each_lens_calls_the_screens_own_function(a_reader, monkeypatch):
    """Quatre lentilles, quatre composeurs. Aucune derivation locale."""
    import core.feedback_review as service

    called: list[str] = []
    monkeypatch.setattr(
        service,
        "list_annotations",
        lambda conn, **k: called.append("list") or {"items": [], "limit": k["limit"]},
    )
    monkeypatch.setattr(
        service,
        "aggregate_feedback",
        lambda conn, **k: called.append("aggregate") or {"items": []},
    )
    monkeypatch.setattr(
        service,
        "list_unresolved_critical_negatives",
        lambda conn, **k: called.append("negatives") or {"items": []},
    )
    monkeypatch.setattr(
        service,
        "list_review_versions",
        lambda conn, **k: called.append("reviews") or [],
    )

    read = _tool("read_feedback")
    read(project_id=PROJECT)
    read(project_id=PROJECT, lens="aggregates")
    read(project_id=PROJECT, lens="critical-negatives")
    read(project_id=PROJECT, lens="reviews", feedback_id="fb_1")
    assert called == ["list", "aggregate", "negatives", "reviews"]


def test_one_reaction_is_read_with_the_pins_that_make_it_readable(a_reader, monkeypatch):
    """« Dans son contexte exact » : sans ses epingles, c'est un avis sur rien."""
    import core.feedback_review as service

    seen: dict = {}

    def _get(conn, *, org_id, project_id, feedback_id):
        seen["feedback_id"] = feedback_id
        return {
            "id": feedback_id,
            "polarity": "negative",
            "target_kind": "render",
            "target_id": "rd_1",
            "review_head": {"state": "unreviewed"},
        }

    monkeypatch.setattr(service, "get_annotation", _get)
    result = _tool("read_feedback")(project_id=PROJECT, feedback_id="fb_1")
    assert seen["feedback_id"] == "fb_1"
    assert _payload(result)["feedback"]["target_kind"] == "render"
    assert "opinion about nothing" in result.content[0].text


def test_an_empty_collection_names_the_gesture_and_never_a_table(a_reader, monkeypatch):
    import core.feedback_review as service

    monkeypatch.setattr(service, "list_annotations", lambda conn, **k: {"items": []})
    text = _tool("read_feedback")(project_id=PROJECT).content[0].text
    assert "submit_feedback" in text
    assert "app." not in text
    assert "migration" not in text.lower()


def test_the_reviews_lens_refuses_without_the_reaction_it_reads(a_reader):
    with pytest.raises(ToolError) as excinfo:
        _tool("read_feedback")(project_id=PROJECT, lens="reviews")
    refusal = _refusal(excinfo)
    assert refusal["code"] == "missing_param"
    assert "feedback_id" in refusal["message"]


# --------------------------------------------------------------------------- #
# 2. Repondre -- une version immuable, et les vocabulaires du service.
# --------------------------------------------------------------------------- #


def test_answering_appends_a_version_through_the_screens_own_function(a_writer, monkeypatch):
    import core.feedback_review as service

    seen: dict = {}

    def _append(conn, *, org_id, project_id, feedback_id, reviewer, payload):
        seen.update(
            {
                "org_id": org_id,
                "feedback_id": feedback_id,
                "reviewer": reviewer,
                "payload": dict(payload),
            }
        )
        return {"status": "created", "version_number": 1}

    monkeypatch.setattr(service, "append_review_version", _append)
    result = _tool("review_feedback")(
        project_id=PROJECT, feedback_id="fb_1", review=dict(A_REVIEW)
    )
    assert seen["org_id"] == ORG
    assert seen["reviewer"] == IDENTITY
    assert seen["payload"]["state"] == "triaged"
    assert _payload(result)["version_number"] == 1
    assert "appends, never edits" in result.content[0].text


def test_a_replayed_retry_key_says_it_was_replayed_and_not_appended_twice(a_writer, monkeypatch):
    import core.feedback_review as service

    monkeypatch.setattr(
        service,
        "append_review_version",
        lambda conn, **k: {"status": "replayed", "version_number": 1},
    )
    text = _tool("review_feedback")(
        project_id=PROJECT, feedback_id="fb_1", review=dict(A_REVIEW)
    ).content[0].text
    assert "replayed, not appended twice" in text


def test_the_closed_vocabularies_are_the_services_own_and_are_never_copied():
    """LE test qui compte : la porte n'a pas sa propre liste.

    Il n'ecrit aucune valeur. Il demande au service la sienne, prend une valeur
    qui n'y est PAS, et exige que la porte la refuse en nommant la liste entiere
    -- exactement ce que le service accepterait demain s'il changeait.
    """
    import core.feedback_review as service
    import core.feedback_review_mcp as door

    served = door._vocabularies()
    assert served["state"] == tuple(service.REVIEW_STATES)
    assert served["affected_dimension"] == tuple(service.AFFECTED_DIMENSIONS)
    assert served["human_verdict"] == tuple(service.HUMAN_VERDICTS)
    assert served["severity"] == tuple(service.SEVERITIES)
    # Et aucune de ces listes n'est ecrite en dur dans le module de la porte.
    source = __import__("pathlib").Path(door.__file__).read_text(encoding="utf-8")
    for value in ("unreviewed", "duplicate", "provenance_correctness", "mcp_app_behavior"):
        assert f'"{value}"' not in source, (
            f"`{value}` est recopie dans la porte : le jour ou le service change, "
            "les deux listes divergent"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "closed"),
        ("affected_dimension", "vibes"),
        ("human_verdict", "maybe"),
        ("severity", "cosmetic"),
    ],
)
def test_a_value_outside_a_closed_vocabulary_is_refused_by_naming_the_whole_set(
    a_writer, monkeypatch, field, value
):
    """Un modele a qui on dit « refuse » devine ; a qui on dit la liste, il reessaie."""
    import core.feedback_review as service

    monkeypatch.setattr(
        service,
        "append_review_version",
        lambda *a, **k: pytest.fail("a refused value must not reach the service"),
    )
    with pytest.raises(ToolError) as excinfo:
        _tool("review_feedback")(
            project_id=PROJECT, feedback_id="fb_1", review={**A_REVIEW, field: value}
        )
    refusal = _refusal(excinfo)
    assert refusal["code"] == "invalid_param"
    assert field in refusal["message"]
    assert "must be one of" in refusal["message"]


def test_a_review_without_a_retry_key_is_refused_before_anything_opens(a_writer, monkeypatch):
    """Sans cle de reprise, une reponse rejouee s'ajouterait deux fois."""
    import core.feedback_review as service

    monkeypatch.setattr(
        service, "append_review_version", lambda *a, **k: pytest.fail("must refuse first")
    )
    payload = dict(A_REVIEW)
    payload.pop("retry_key")
    with pytest.raises(ToolError) as excinfo:
        _tool("review_feedback")(project_id=PROJECT, feedback_id="fb_1", review=payload)
    assert _refusal(excinfo)["code"] == "missing_param"


def test_this_door_never_leaves_a_reaction():
    """`submit_feedback` est le seul ecrivain d'une reaction, et il n'est pas ici.

    Un second ecrivain de la meme table par un autre chemin est exactement la
    derive qu'AD-1 interdit.
    """
    import core.feedback_review_mcp as door

    source = __import__("pathlib").Path(door.__file__).read_text(encoding="utf-8")
    for writer in ("submit_exact_feedback", "submit_ai_path_step_feedback"):
        assert f"{writer}(" not in source, f"{writer} est appele ici"


# --------------------------------------------------------------------------- #
# 3. Les refus de forme et les rangs declares.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kwargs", "code"),
    [
        ("read_feedback", {"project_id": ""}, "missing_param"),
        ("read_feedback", {"project_id": PROJECT, "lens": "everything"}, "unknown_lens"),
        ("review_feedback", {"project_id": "", "feedback_id": "fb_1", "review": A_REVIEW}, "missing_param"),
        ("review_feedback", {"project_id": PROJECT, "feedback_id": "", "review": A_REVIEW}, "missing_param"),
        ("review_feedback", {"project_id": PROJECT, "feedback_id": "fb_1", "review": {}}, "missing_param"),
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
    read = _declaration("read_feedback")
    assert (read["profile"], read["effect"], read["confirmation_mode"]) == (
        "insights",
        "read",
        "none",
    )
    write = _declaration("review_feedback")
    assert (write["profile"], write["effect"], write["confirmation_mode"]) == (
        "operations",
        "confirmed_write",
        "human",
    )
