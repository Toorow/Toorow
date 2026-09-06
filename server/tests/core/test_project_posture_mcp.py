"""`get_project_posture`: declared, project-scoped, non-disclosing, read-only.

Fully offline. No database, no MCP transport: the access seam and the overview
composer are replaced, the connection is a fake, and every assertion is about
what the tool DOES with what it is given.

What is pinned here, and why each one:

  (a) the tool goes through `register_profiled` with profile / effect / data
      class / confirmation mode, and NO bare `mcp.tool` appears in the module. A
      bare registration escapes `CapabilityProfileMiddleware` entirely, and since
      AD-43 an undeclared tool reaches nobody at all.
  (b) it is PROJECT SCOPED: the guard runs at the `view` floor before any read,
      and it runs on the project the caller named.
  (c) a caller outside the project is refused WITHOUT DISCLOSURE -- the same
      canonical `project_not_found` envelope as a project that does not exist,
      carrying no name, no state, no evidence. A guard that cannot run refuses
      the same way (fail-closed).
  (d) it reports the FIRST-PUBLICATION state the Overview shows, from the same
      composer, with the owner the amendment of 2026-08-17 ratified -- the
      Renders collection, never a Datastream action the console cannot open.
  (e) it is a READ: `can_edit=False`, and nothing is committed.
  (f) the attention queue is bounded and states its TRUE total.

File paths are anchored on the repository root, never on the working directory
(AI-136: a relative path renders two different verdicts depending on where pytest
was launched from).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_MODULE_PATH = REPO_ROOT / "server" / "core" / "project_posture_mcp.py"

TOOL_NAME = "get_project_posture"
PROJECT = "proj_EXAMPLE"
MEMBER = "owner@example.com"
OUTSIDER = "someone@example.com"


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
    """Run `project_posture_mcp.register` and return (recorder, handlers-by-name)."""
    from core import mcp_profiles, project_posture_mcp

    recorder = _Recorder()
    handlers: dict = {}
    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        handlers[kwargs.get("name") or handler.__name__] = handler
        return real(mcp, handler, **kwargs)

    monkeypatch.setattr(mcp_profiles, "register_profiled", spy)
    project_posture_mcp.register(recorder)
    return recorder, handlers


class _Conn:
    """A connection double the ACQUISITION SEAM can arm.

    `_guard_project_view` and `get_project_posture` acquire through
    `core.db.request_connection` since 2026-08-21, and that seam runs `SET ROLE`
    and two `set_config` statements on a CURSOR of the connection it returns. A
    double that only answered `commit` made the guard fail closed on an
    `AttributeError` and every test here read `project_not_found`.
    """

    def __init__(self):
        self.commit = MagicMock()
        self.rollback = MagicMock()

    def cursor(self):
        return _AccessContextCursor()


class _AccessContextCursor:
    """Answers the three statements the access context installs, and nothing else.

    `fetchone()` returns None on purpose: `core/db.py` treats a reply that is not
    a two-column row as "this is a double that did not script the question" and
    stays silent rather than claiming the floor fell. Scripting a fake ('on',
    identity) pair instead would make this file assert an isolation it never
    exercised.
    """

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return None


@contextmanager
def _connection():
    yield _Conn()


def _renders_owner() -> dict:
    """The owner the amendment ratified: the Renders collection, no action."""
    return {
        "surface": "project",
        "workspace": "analyze",
        "section": "renders",
        "global_surface": None,
        "global_section": None,
        "object_type": None,
        "object_id": None,
        "tab": None,
        "action": None,
        "version_id": None,
        "evidence_id": None,
    }


def _overview_envelope(*, first_value_id=None, attention_items=7) -> dict:
    """A composer envelope shaped like the real one, bounded to what is read."""
    return {
        "schema_version": "project-overview.v1",
        "project": {"id": PROJECT, "name": "Example Project", "as_of": "2026-08-17T09:00:00+00:00"},
        "posture": {
            "operational_health": {
                "state": "unknown", "explanation": "No published evidence.",
                "evidence_horizon": None,
            },
            "trust_readiness": {
                "state": "degraded", "explanation": "Governance readiness is partial.",
                "evidence_horizon": None,
            },
            "business_signals": {
                "state": "unknown", "explanation": "No persisted business signal.",
                "evidence_horizon": None,
            },
            "limiting_dimension": "operational_health",
        },
        "readiness": {
            "schema_version": "project-readiness.v1",
            "version": "abcdef0123456789abcdef01",
            "project_foundation": {"state": "ready", "evidence_ref": "cfg_1", "owner": {}},
            "source": {"state": "ready", "evidence_ref": "src_1", "owner": {}},
            "datastream": {"state": "ready", "evidence_ref": "ds_1", "owner": {}},
            "first_value": {
                "state": "ready" if first_value_id else "blocked",
                "evidence_ref": first_value_id,
                "owner": _renders_owner(),
            },
        },
        "next_action": {
            "label": "Publish a first result", "permitted": True, "handoff": None,
            "cause": "No publication yet.", "owner": _renders_owner(),
        },
        "attention": {
            "items": [
                {
                    "id": f"data:ds_1:reason_{index}",
                    "cause": f"cause {index}",
                    "impact": ["Project data trust is limited."],
                    "scope": ["Stream"],
                    "status": "unknown",
                    "owner": _renders_owner(),
                }
                for index in range(attention_items)
            ],
            "total": attention_items,
            "has_more": False,
        },
        # Deliberately NOT projected by the tool.
        "coverage": [{"kind": "data", "key": "publication"}],
        "outcomes": {"status": "empty", "items": []},
        "changes": {"status": "empty", "items": []},
    }


@contextmanager
def _environment(*, allowed=True, identity=MEMBER, envelope=None, guard_raises=None,
                 compose_raises=None, recorder=None):
    """Patch the access seam, the composer, the connection and the identity."""
    from core import project_access, project_overview, project_posture_mcp

    decision = SimpleNamespace(allowed=allowed, org_id="org_EXAMPLE" if allowed else None)

    def _guard(*_args, **_kwargs):
        if guard_raises is not None:
            raise guard_raises
        if recorder is not None:
            recorder.append(_kwargs)
        return decision

    def _compose(project_id, conn, *, actor, can_edit=False):
        if compose_raises is not None:
            raise compose_raises
        if recorder is not None:
            recorder.append({"project_id": project_id, "actor": actor, "can_edit": can_edit})
        return envelope if envelope is not None else _overview_envelope()

    with (
        patch.object(project_access, "resolve_strict_resource_access", _guard),
        patch.object(project_overview, "compose_project_overview", _compose),
        patch.object(project_posture_mcp, "_identity", return_value=identity),
        patch("core.db.get_connection", side_effect=_connection),
    ):
        yield


def _data(result) -> dict:
    """Unwrap the AD-1 envelope the tool returns."""
    return result.structured_content["data"]


def _error(excinfo) -> dict:
    """Unwrap the canonical `{code, message}` a ToolError carries."""
    return json.loads(str(excinfo.value))


# ---------------------------------------------------------------------------
# (a) declared to the capability catalog -- no bare mcp.tool
# ---------------------------------------------------------------------------


def test_the_tool_is_declared_read_only_under_insights(monkeypatch):
    from core import mcp_profiles

    _register(monkeypatch)
    declarations = {d.name: d for d in mcp_profiles.registered_declarations()}

    assert set(declarations) == {TOOL_NAME}
    declaration = declarations[TOOL_NAME]
    assert declaration.profile == "insights", (
        "the posture read is the non-risky, always-discoverable profile"
    )
    assert declaration.effect == "read"
    assert declaration.data_class == "operational"
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
    """`mcp.tool(` anywhere in this module would escape the capability middleware."""
    source = MCP_MODULE_PATH.read_text(encoding="utf-8")
    assert "mcp.tool(" not in source
    assert "@mcp.tool" not in source
    assert source.count("register_profiled(") == 1, (
        "exactly one declared registration -- a second tool must be declared too"
    )


def test_the_tool_name_carries_no_connector_name():
    """AD-42: a catalogue name names a question, never a vendor."""
    for vendor in ("google", "meta", "facebook", "tiktok", "linkedin", "amazon", "bing"):
        assert vendor not in TOOL_NAME


# ---------------------------------------------------------------------------
# (b) + (c) project scope, and a refusal that discloses nothing
# ---------------------------------------------------------------------------


def test_the_guard_runs_at_the_view_floor_on_the_named_project(monkeypatch):
    _recorder, handlers = _register(monkeypatch)
    seen: list = []

    with _environment(recorder=seen):
        handlers[TOOL_NAME](project_id=PROJECT)

    guard_call = seen[0]
    assert guard_call["project_id"] == PROJECT
    assert guard_call["minimum_capability"] == "view", (
        "a posture read must not require more than view, nor less"
    )


def test_a_caller_outside_the_project_is_refused_without_disclosure(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)

    with _environment(allowed=False, identity=OUTSIDER), pytest.raises(ToolError) as excinfo:
        handlers[TOOL_NAME](project_id=PROJECT)

    error = _error(excinfo)
    # The CANONICAL envelope, shared with every other project-scoped tool: a
    # distinct one for "forbidden" is an enumeration oracle.
    assert error["code"] == "project_not_found"
    # NOTHING about the project leaks: not its name, not its posture, not the
    # fact that it exists at all.
    assert "Example Project" not in error["message"]
    assert PROJECT not in error["message"]
    assert "blocked" not in error["message"] and "denied" not in error["message"].lower()


def test_the_composer_is_never_reached_when_access_is_denied(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seen: list = []

    with _environment(allowed=False, recorder=seen), pytest.raises(ToolError):
        handlers[TOOL_NAME](project_id=PROJECT)

    assert all("actor" not in call for call in seen), "a denied call composed the overview anyway"


def test_a_guard_that_cannot_run_refuses_the_same_way(monkeypatch):
    """Fail-closed: an authorization seam that raised has not granted anything."""
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)

    with (
        _environment(guard_raises=RuntimeError("scope database is down")),
        pytest.raises(ToolError) as excinfo,
    ):
        handlers[TOOL_NAME](project_id=PROJECT)

    error = _error(excinfo)
    assert error["code"] == "project_not_found"
    assert "down" not in error["message"], "the refusal must not describe our infrastructure"


def test_a_missing_project_is_refused_before_any_seam(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seen: list = []

    with _environment(recorder=seen), pytest.raises(ToolError) as excinfo:
        handlers[TOOL_NAME](project_id="   ")

    assert _error(excinfo)["code"] == "missing_param"
    assert seen == []


# ---------------------------------------------------------------------------
# (d) the first-publication state the Overview shows
# ---------------------------------------------------------------------------


def test_an_unpublished_project_says_so_and_owns_the_render_destination(monkeypatch):
    _recorder, handlers = _register(monkeypatch)

    with _environment():
        data = _data(handlers[TOOL_NAME](project_id=PROJECT))

    first = data["first_publication"]
    assert first["published"] is False
    assert first["state"] == "blocked"
    assert first["evidence_ref"] is None
    # THE REPOINTING, READ BY AN AGENT. The owner is the share/render
    # destination, never a Datastream action the console declares nowhere.
    assert first["owner"]["workspace"] == "analyze"
    assert first["owner"]["section"] == "renders"
    assert first["owner"]["action"] is None
    assert first["owner"]["object_type"] is None


def test_a_published_project_names_the_publication_that_proves_it(monkeypatch):
    _recorder, handlers = _register(monkeypatch)

    with _environment(envelope=_overview_envelope(first_value_id="exec_1")):
        data = _data(handlers[TOOL_NAME](project_id=PROJECT))

    assert data["first_publication"]["published"] is True
    assert data["first_publication"]["state"] == "ready"
    assert data["first_publication"]["evidence_ref"] == "exec_1"


def test_the_three_posture_dimensions_stay_separate(monkeypatch):
    """`overview.md` forbids a composite score: an agent must see which one limits."""
    _recorder, handlers = _register(monkeypatch)

    with _environment():
        data = _data(handlers[TOOL_NAME](project_id=PROJECT))

    posture = data["posture"]
    assert posture["operational_health"]["state"] == "unknown"
    assert posture["trust_readiness"]["state"] == "degraded"
    assert posture["business_signals"]["state"] == "unknown"
    assert posture["limiting_dimension"] == "operational_health"
    assert "score" not in posture


def test_the_four_readiness_components_ride_with_their_version(monkeypatch):
    _recorder, handlers = _register(monkeypatch)

    with _environment():
        data = _data(handlers[TOOL_NAME](project_id=PROJECT))

    readiness = data["readiness"]
    assert set(readiness["components"]) == {
        "project_foundation", "source", "datastream", "first_value",
    }
    assert readiness["version"] == "abcdef0123456789abcdef01"


def test_the_attention_queue_is_bounded_and_states_its_true_total(monkeypatch):
    """A bounded list that hid its denominator would be a smaller Project."""
    _recorder, handlers = _register(monkeypatch)

    with _environment(envelope=_overview_envelope(attention_items=7)):
        data = _data(handlers[TOOL_NAME](project_id=PROJECT))

    assert len(data["attention"]["items"]) == 5
    assert data["attention"]["total"] == 7
    assert data["attention"]["has_more"] is True


def test_the_summary_line_states_the_first_publication_state(monkeypatch):
    _recorder, handlers = _register(monkeypatch)

    with _environment():
        result = handlers[TOOL_NAME](project_id=PROJECT)

    summary = result.content[0].text
    assert "NO first publication yet" in summary
    assert "operational_health" in summary


# ---------------------------------------------------------------------------
# (e) it is a read
# ---------------------------------------------------------------------------


def test_the_tool_composes_without_edit_rights_and_commits_nothing(monkeypatch):
    _recorder, handlers = _register(monkeypatch)
    seen: list = []

    with _environment(recorder=seen):
        handlers[TOOL_NAME](project_id=PROJECT)

    compose_call = next(call for call in seen if "actor" in call)
    assert compose_call["can_edit"] is False, (
        "a read must not ask the composer for the edit-gated affordances"
    )


def test_the_envelope_leaves_the_heavy_zones_behind(monkeypatch):
    """A catalogue entry that returns everything is a page, not a tool."""
    _recorder, handlers = _register(monkeypatch)

    with _environment():
        data = _data(handlers[TOOL_NAME](project_id=PROJECT))

    for zone in ("coverage", "outcomes", "changes"):
        assert zone not in data


def test_a_composer_failure_is_named_and_not_reported_as_an_empty_project(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)

    with (
        _environment(compose_raises=RuntimeError("boom")),
        pytest.raises(ToolError) as excinfo,
    ):
        handlers[TOOL_NAME](project_id=PROJECT)

    assert _error(excinfo)["code"] == "seam_unavailable"
