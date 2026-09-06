"""AI-67.1 -- the access decision on `get_procedure` governs the SKILL'S BODY.

WHAT THIS FILE PINS, AND WHY IT COULD NOT LIVE IN THE CONFORMANCE GUARD. The
53.1 guard derives, from the source of every `core/*.py`, whether a
project-scoped MCP tool resolves access. It answers "a decision is taken". It
cannot answer "the decision governs what is served" -- and that was exactly the
gap: `get_procedure` resolved access inside the block that builds the open
remarks, so a refusal removed the remarks and the Skill went out anyway. Body,
frontmatter, standardized sequence and MDM references, to anyone who could name
the project.

The property is therefore measured on the payload, both ways:

  * refused  -> nothing of the Skill is served, and the refusal carries the same
    envelope an absent project carries (comparing two refusals must not teach
    that a project exists -- `context-hub.md`'s non-disclosing rule, and the
    reason REFUSAL was chosen over a platform-only degradation);
  * granted  -> the body IS served, so the repair is a guard and not a wall.

NO POSTGRES. The decision point is `resolve_strict_resource_access`, doubled
here; what is under test is that the tool obeys it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from core import main as core_main
from core.project_resolver import PROJECT_NOT_FOUND_CODE
from fastmcp.exceptions import ToolError

_SECRET_BODY = "## Step 1\nOpen the neighbour's ledger and read every line."

#: The Skill the store would return if the read were ever reached.
_A_SKILL = {
    "id": "proc_EXAMPLE",
    "name": "close-the-month",
    "description": "How this project closes its month",
    "frontmatter_yaml": "name: close-the-month\nsteps:\n  - kind: read\n",
    "body_md": _SECRET_BODY,
    "version_number": 3,
    "project_id": "proj_neighbour",
}


@contextmanager
def _a_connection_that_opens(_identity=None):
    yield MagicMock()


@pytest.fixture()
def a_caller(monkeypatch):
    """An authenticated subject, with the database doubled out of the way.

    `TOOROW_AUTH_MODE` must be armed: the `disabled` mode carries the single
    self-host operator carve-out, and a test that forgot this line would measure
    the carve-out instead of the guard.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    token = SimpleNamespace(claims={"sub": "person_stranger"}, client_id="cli")
    monkeypatch.setattr(core_main, "get_access_token", lambda: token)
    monkeypatch.setattr(
        "core.main._resolve_project", lambda pid, identity=None: pid or "proj_neighbour"
    )
    monkeypatch.setattr("core.db.request_connection", _a_connection_that_opens)
    monkeypatch.setattr("core.db.get_connection", _a_connection_that_opens)
    return token


def _denied(*_args, **_kwargs):
    return SimpleNamespace(
        allowed=False, org_id=None, reason="grant_required", capability=None
    )


def _granted(*_args, **_kwargs):
    return SimpleNamespace(allowed=True, org_id="org_x", reason=None, capability="view")


def test_a_refused_caller_never_receives_the_skill_body(a_caller, monkeypatch):
    """The whole payload is refused, not a sub-field of it."""
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)

    def _must_not_read(*_a, **_k):
        raise AssertionError("the Skill was read despite the refusal")

    with patch("core.context_search.get_procedure_by_name", _must_not_read):
        with pytest.raises(ToolError) as refused:
            core_main.get_procedure(name="close-the-month", project_id="proj_neighbour")

    rendered = str(refused.value)
    assert PROJECT_NOT_FOUND_CODE in rendered, (
        "the refusal does not carry the envelope of an absent project: "
        f"{rendered!r}. A distinct 'forbidden' envelope is an enumeration oracle."
    )
    assert _SECRET_BODY not in rendered
    assert "close-the-month" not in rendered


def test_the_refusal_and_an_absent_project_are_one_answer(a_caller, monkeypatch):
    """Comparing two refusals must not teach that the project exists."""
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _denied)
    with pytest.raises(ToolError) as denied:
        core_main.get_procedure(name="close-the-month", project_id="proj_neighbour")

    def _explode(*_a, **_k):
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _explode)
    with pytest.raises(ToolError) as unavailable:
        core_main.get_procedure(name="close-the-month", project_id="proj_neighbour")

    def _envelope_of(exc: ToolError) -> str:
        text = str(exc)
        try:
            return json.dumps(json.loads(text), sort_keys=True)
        except Exception:  # noqa: BLE001 -- transport shape is not the subject
            return text

    assert _envelope_of(denied.value) == _envelope_of(unavailable.value)


def test_a_permitted_caller_still_receives_the_skill_body(a_caller, monkeypatch):
    """The other half: without it, an unconditional `raise` would pass the rest."""
    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _granted)

    with (
        patch("core.context_search.get_procedure_by_name", return_value=dict(_A_SKILL)),
        patch("core.mdm_references.resolve", return_value={"resolved": [], "unresolved": []}),
        patch("core.mdm_references.tags_of", return_value=[]),
        patch("core.context_review.list_open", return_value=[]),
    ):
        result = core_main.get_procedure(
            name="close-the-month", project_id="proj_neighbour"
        )

    served = result.structured_content["data"]["procedure"]
    assert served["body_md"] == _SECRET_BODY
    assert served["name"] == "close-the-month"


def test_the_guard_demands_view_and_runs_before_the_read(a_caller, monkeypatch):
    """The rank asked for, and the order it is asked in.

    A read demanding `edit` would refuse legitimate readers; a guard that runs
    after the read has already spent the query it exists to refuse.
    """
    seen: list[str] = []

    def _record(*_a, **kwargs):
        seen.append(kwargs.get("minimum_capability", "view"))
        return _denied()

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _record)
    with patch("core.context_search.get_procedure_by_name") as read:
        with pytest.raises(ToolError):
            core_main.get_procedure(name="close-the-month", project_id="proj_neighbour")

    assert seen and seen[0] == "view", f"capability demanded: {seen}"
    assert not read.called, "the Skill was read before/despite the access decision"
