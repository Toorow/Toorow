"""The client label of a dimension, reachable by a model.

Traced 2026-08-03 for the owner's sentence *"give understandable names"*
(story 27.9, `capability-trace`):

    API   4 routes  `dimension_lineage_api.py:393-396`
    UI    ABSENT    `grep -rn "dimension-lineage" ui/admin/src` -> nothing
    MCP   ABSENT    no tool named the verb

Served, and reachable by nobody: a person could not rename a dimension and
neither could a model. These tests hold the model's door open and, more
importantly, hold it onto the SAME functions the REST handlers call — a second
implementation of the cascade would give two answers to "what is this dimension
called", which is the defect a client label exists to remove.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import core.dimension_labels_mcp as mod


@contextmanager
def _a_connection_that_opens(_identity):
    """Une connexion qui s'ouvre, pour que la decision d'appartenance soit atteinte.

    Sans elle la garde ouvre une vraie connexion, echoue, et refuse -- FERME,
    donc correctement, mais ce test serait alors rouge pour l'absence de
    Postgres et non pour ce qu'il mesure.
    """
    yield MagicMock()


class _Recorder:
    """Captures what `register` declares, without a live MCP server."""

    def __init__(self):
        self.tools = {}

    def register_profiled(self, _mcp, fn, **kw):
        self.tools[fn.__name__] = {"fn": fn, **kw}


def _register(monkeypatch):
    rec = _Recorder()
    import core.mcp_profiles as profiles

    monkeypatch.setattr(profiles, "register_profiled", rec.register_profiled)
    mod.register(object())
    return rec.tools


def test_both_doors_are_declared_with_the_right_effect(monkeypatch):
    tools = _register(monkeypatch)
    assert set(tools) == {"read_dimension_labels", "set_dimension_label"}
    assert tools["read_dimension_labels"]["effect"] == "read"
    # The label travels to every render, so renaming changes what a reader
    # believes a number counts. That is not a preference toggle.
    assert tools["set_dimension_label"]["effect"] == "confirmed_write"
    assert tools["set_dimension_label"]["confirmation_mode"] == "human"


def test_a_read_without_a_scope_is_refused_rather_than_guessed(monkeypatch):
    tools = _register(monkeypatch)
    out = tools["read_dimension_labels"]["fn"]()
    assert out["error"] == "missing_scope"


def test_the_read_delegates_to_the_one_resolver(monkeypatch):
    seen = {}
    import core.dimension_conformance as conformance

    def fake(*, org_id, project_id=None):
        seen.update(org_id=org_id, project_id=project_id)
        return {"country": {"display_label": "Pays", "scope_level": "org"}}

    monkeypatch.setattr(conformance, "resolve_dimension_labels", fake)
    tools = _register(monkeypatch)
    out = tools["read_dimension_labels"]["fn"](org_id="org_EXAMPLE")
    assert seen == {"org_id": "org_EXAMPLE", "project_id": None}
    assert out["labelled_count"] == 1
    assert out["labels"]["country"]["display_label"] == "Pays"


def test_an_unknown_scope_level_is_refused_before_any_write(monkeypatch):
    called = []
    import core.dimension_conformance as conformance

    monkeypatch.setattr(conformance, "set_dimension_label", lambda **kw: called.append(kw))
    tools = _register(monkeypatch)
    out = tools["set_dimension_label"]["fn"]("country", "Pays", "workspace")
    assert out["error"] == "invalid_scope_level"
    assert called == []


def test_the_write_delegates_and_never_renames_the_identifier(monkeypatch):
    captured = {}
    import core.dimension_conformance as conformance

    def fake(**kw):
        captured.update(kw)
        return {"changed": True}

    monkeypatch.setattr(conformance, "set_dimension_label", fake)
    monkeypatch.setattr(mod, "_identity", lambda: "owner@example.com")
    # 2026-08-21 : la branche `org` de cet outil ne traversait AUCUN controle de
    # portee, et ce test le prouvait sans le savoir -- il ecrivait chez
    # `org_EXAMPLE` sans qu'une seule question d'appartenance soit posee. La
    # garde est desormais franchie EXPLICITEMENT, en accordant owner/admin :
    # ce que ce test mesure est la delegation, et l'isolation a le sien
    # (`tests/isolation/test_mcp_tool_scope_refusal.py`).
    monkeypatch.setattr(
        "core.project_access.identity_can_manage_org", lambda *_a, **_k: True
    )
    monkeypatch.setattr("core.db.request_connection", _a_connection_that_opens)
    tools = _register(monkeypatch)
    tools["set_dimension_label"]["fn"]("country", "Pays", "org", org_id="org_EXAMPLE")

    # The stable identifier is passed through untouched: a rename that moved it
    # would break every pinned Result referencing it.
    assert captured["canonical_dimension"] == "country"
    assert captured["display_label"] == "Pays"
    assert captured["identity"] == "owner@example.com"
