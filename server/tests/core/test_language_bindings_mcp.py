"""Chantier 67-24 -- the language family on the MCP surface.

WHAT THIS FILE PINS. `core.language_dimensions` protects a distinction an agent
is especially likely to destroy: three dimensions share the word "language", and
the gap between what was TARGETED and what was OBSERVED is an information, not
an error to reconcile. Until this tool, no agent could read that family at all --
the whole binding lifecycle had zero production callers on 2026-08-17.

These tests hold five things:
  (a) the tool is DECLARED to the capability catalog -- no bare `mcp.tool`;
  (b) it is an Insights READ, the only profile a non-writing tool may carry;
  (c) it reads through the SAME store functions the REST surface writes, keyed on
      the same scope triplet -- a reader that dropped `org_id` would silently
      answer "no bindings" for a project that has them;
  (d) the family and its three natures travel with the answer, so the model is
      told why the dimensions never combine;
  (e) the list is bounded and always states its true total.

Offline throughout: the store is stubbed, so nothing here needs Postgres. The
guard itself is proven against a real refusal in
`tests/isolation/test_mcp_tool_scope_refusal.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_MODULE_PATH = REPO_ROOT / "server" / "core" / "language_bindings_mcp.py"

TOOL_NAME = "read_language_bindings"
PROJECT = "proj_EXAMPLE"
ORG = "org_EXAMPLE"


@pytest.fixture(autouse=True)
def _clean_registry():
    from core import mcp_profiles

    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


class _Recorder:
    """A stand-in mcp that records every `tool()` registration verbatim."""

    def __init__(self):
        self.tool_calls: list[tuple[str, dict]] = []

    def tool(self, handler, **kwargs):
        name = kwargs.get("name") or handler.__name__
        self.tool_calls.append((name, kwargs))
        return SimpleNamespace(name=name, tags=kwargs.get("tags"), meta=kwargs.get("meta"))


def _register(monkeypatch) -> tuple[_Recorder, dict]:
    """Run `language_bindings_mcp.register` and return (recorder, handlers)."""
    from core import language_bindings_mcp, mcp_profiles

    recorder = _Recorder()
    handlers: dict = {}
    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        handlers[kwargs.get("name") or handler.__name__] = handler
        return real(mcp, handler, **kwargs)

    monkeypatch.setattr(mcp_profiles, "register_profiled", spy)
    language_bindings_mcp.register(recorder)
    return recorder, handlers


def _binding(**overrides) -> dict:
    row = {
        "id": "dfb_example",
        "connector": "example-ads",
        "report_id": "daily",
        "source_field": "creativeLanguage",
        "canonical_dimension": "content_language",
        "status": "confirmed",
        "reviewed_by": "owner@example.com",
        "reviewed_at": "2026-08-17T10:00:00+00:00",
        "evidence_quote": "the language of the creative",
    }
    row.update(overrides)
    return row


def _tool(monkeypatch):
    _recorder, handlers = _register(monkeypatch)
    return handlers[TOOL_NAME]


def _data(result) -> dict:
    return result.structured_content["data"]


def _error(excinfo) -> dict:
    return json.loads(str(excinfo.value))


def _serve(monkeypatch, rows, applied=None, org=ORG):
    """Stub the store + the guard, and return the recorded list_bindings kwargs."""
    from core import language_bindings_mcp, language_dimensions

    seen: dict = {}

    def _list_bindings(**kwargs):
        seen.update(kwargs)
        return list(rows)

    monkeypatch.setattr(language_dimensions, "list_bindings", _list_bindings)
    monkeypatch.setattr(
        language_dimensions,
        "resolve_field_bindings",
        lambda **_k: applied if applied is not None else {},
    )
    # `_org_of` takes the identity it acquires with since 2026-08-21: it reads
    # `app.projects`, which carries an RLS policy, so the connection under it
    # must carry the caller. The double follows the signature rather than
    # pinning the old one.
    monkeypatch.setattr(language_bindings_mcp, "_org_of", lambda _p, _identity: org)
    monkeypatch.setattr(language_bindings_mcp, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        "core.mcp_scope.refuse_unless_project_scope", lambda *_a, **_k: None
    )
    return seen


# ---------------------------------------------------------------------------
# (a) + (b) declared to the catalog, as an Insights read
# ---------------------------------------------------------------------------


def test_the_tool_is_declared_read_only_under_insights(monkeypatch):
    from core import mcp_profiles

    _register(monkeypatch)
    declarations = {d.name: d for d in mcp_profiles.registered_declarations()}

    assert set(declarations) == {TOOL_NAME}
    declaration = declarations[TOOL_NAME]
    assert declaration.profile == "insights", (
        "reading which dimension a column carries is a safe read, not governance"
    )
    assert declaration.effect == "read"
    assert declaration.confirmation_mode == "none"
    assert len(mcp_profiles.validate_catalog()) == 1


def test_the_registration_carries_capability_metadata(monkeypatch):
    """A registration without `meta` is a tool the middleware cannot filter."""
    recorder, _handlers = _register(monkeypatch)

    assert {name for name, _kwargs in recorder.tool_calls} == {TOOL_NAME}
    _name, kwargs = recorder.tool_calls[0]
    meta = kwargs.get("meta") or {}
    assert not {"profile", "effect", "data_class", "confirmation_mode"} - set(meta)


def test_the_module_registers_nothing_bare():
    """`mcp.tool(` anywhere here would escape the capability middleware."""
    source = MCP_MODULE_PATH.read_text(encoding="utf-8")
    assert "mcp.tool(" not in source
    assert "@mcp.tool" not in source
    assert source.count("register_profiled(") == 1


def test_the_tool_name_carries_no_connector_name():
    """AD-42: a catalogue name names a question, never a vendor."""
    for vendor in ("google", "meta", "facebook", "tiktok", "linkedin", "amazon", "bing"):
        assert vendor not in TOOL_NAME


def test_the_module_writes_nothing():
    """An Insights read that could confirm a binding would decide what a number means."""
    source = MCP_MODULE_PATH.read_text(encoding="utf-8")
    for writer in ("confirm_binding", "reject_binding", "persist_binding_proposals"):
        assert writer not in source, f"{writer} is a human act on the REST surface"


# ---------------------------------------------------------------------------
# (c) it reads the store the REST surface writes, on the SAME triplet
# ---------------------------------------------------------------------------


def test_the_read_is_keyed_on_the_same_scope_triplet_the_writer_used(monkeypatch):
    """The bug this pins: `list_bindings` matches `COALESCE(org_id, '')`.

    A reader that passed no `org_id` would look for rows stored with a NULL org
    and answer "no bindings" for a project whose bindings all carry one -- and the
    RLS policy of migration 274 makes a NULL org a row belonging to no tenant, so
    dropping it on the WRITE side is not an option either.
    """
    from core.language_dimensions import SCOPE_PROJECT

    seen = _serve(monkeypatch, [_binding()])
    _tool(monkeypatch)(project_id=PROJECT)

    assert seen["scope_level"] == SCOPE_PROJECT
    assert seen["project_id"] == PROJECT
    assert seen["org_id"] == ORG


def test_a_blank_project_is_refused_before_any_read(monkeypatch):
    from core import language_dimensions

    monkeypatch.setattr(
        language_dimensions,
        "list_bindings",
        MagicMock(side_effect=AssertionError("read before the shape check")),
    )
    with pytest.raises(Exception) as excinfo:
        _tool(monkeypatch)(project_id="   ")
    assert _error(excinfo)["code"] == "missing_param"


def test_a_store_failure_is_named_and_never_read_as_empty(monkeypatch):
    """An unreadable store must not look like a project with no bindings."""
    from core import language_bindings_mcp, language_dimensions

    monkeypatch.setattr(
        language_dimensions,
        "list_bindings",
        MagicMock(side_effect=RuntimeError("no connection")),
    )
    monkeypatch.setattr(language_bindings_mcp, "_org_of", lambda _p, _identity: ORG)
    monkeypatch.setattr(language_bindings_mcp, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        "core.mcp_scope.refuse_unless_project_scope", lambda *_a, **_k: None
    )

    with pytest.raises(Exception) as excinfo:
        _tool(monkeypatch)(project_id=PROJECT)
    assert _error(excinfo)["code"] == "seam_unavailable"


def test_the_status_filter_travels_to_the_store(monkeypatch):
    seen = _serve(monkeypatch, [])
    _tool(monkeypatch)(project_id=PROJECT, status="proposed")
    assert seen["status"] == "proposed"

    seen_all = _serve(monkeypatch, [])
    _tool(monkeypatch)(project_id=PROJECT)
    assert seen_all["status"] is None, "no filter means every status, not the empty one"


# ---------------------------------------------------------------------------
# (d) the family travels with the answer
# ---------------------------------------------------------------------------


def test_the_three_dimensions_and_their_natures_travel_with_every_answer(monkeypatch):
    """The model must be told WHY the three never combine, not just their names."""
    _serve(monkeypatch, [_binding()])
    data = _data(_tool(monkeypatch)(project_id=PROJECT))

    family = {entry["dimension"]: entry for entry in data["family"]}
    assert set(family) == {
        "audience_language",
        "content_language",
        "targeting_language",
    }
    assert family["audience_language"]["nature"] == "observed_on_person"
    assert family["content_language"]["nature"] == "property_of_asset"
    assert family["targeting_language"]["nature"] == "declared_intent"
    assert all(entry["definition"] for entry in family.values())


def test_the_summary_states_the_dimensions_are_never_combined(monkeypatch):
    _serve(
        monkeypatch,
        [_binding()],
        applied={("example-ads", "daily", "creativeLanguage"): "content_language"},
    )
    result = _tool(monkeypatch)(project_id=PROJECT)
    text = result.content[0].text
    assert "never summed or compared" in text


def test_an_empty_project_says_why_rather_than_returning_a_bare_zero(monkeypatch):
    _serve(monkeypatch, [])
    result = _tool(monkeypatch)(project_id=PROJECT)
    data = _data(result)

    assert data["total"] == 0
    assert data["bindings"] == []
    # The family is still offered: an empty list must name what could be declared.
    assert len(data["family"]) == 3
    assert "No language binding is declared" in result.content[0].text


# ---------------------------------------------------------------------------
# (e) bounded, and never hiding its own denominator
# ---------------------------------------------------------------------------


def test_a_long_list_is_bounded_but_states_its_true_total(monkeypatch):
    from core.language_bindings_mcp import _BINDING_LIMIT

    rows = [_binding(id=f"dfb_{i}", source_field=f"field_{i}") for i in range(_BINDING_LIMIT + 7)]
    _serve(monkeypatch, rows)
    data = _data(_tool(monkeypatch)(project_id=PROJECT))

    assert len(data["bindings"]) == _BINDING_LIMIT
    assert data["total"] == _BINDING_LIMIT + 7, "a truncated list never shrinks the project"
    assert data["has_more"] is True


def test_only_confirmed_bindings_count_as_applied(monkeypatch):
    """A proposed row is stored and listed, but it changes no reading."""
    rows = [_binding(status="proposed"), _binding(id="dfb_2", source_field="f2")]
    _serve(monkeypatch, rows, applied={("example-ads", "daily", "f2"): "content_language"})
    data = _data(_tool(monkeypatch)(project_id=PROJECT))

    assert data["total"] == 2
    assert data["applied"] == 1


def test_a_row_carries_no_evidence_quote_to_the_model_channel(monkeypatch):
    """The envelope is budgeted: the catalogue pays for every byte it ships."""
    _serve(monkeypatch, [_binding()])
    data = _data(_tool(monkeypatch)(project_id=PROJECT))

    assert "evidence_quote" not in data["bindings"][0]
    assert data["bindings"][0]["canonical_dimension"] == "content_language"
