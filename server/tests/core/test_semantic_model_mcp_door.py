"""The MCP door onto the Semantic Model change set calls the console's own path.

WHAT THIS FILE EXISTS FOR. `known-debt.json` recorded, on 2026-08-25, the residue
that retiring `metric_definition_upsert` made visible: *"an agent has no MCP door
onto the Semantic Model change sets -- governance_mcp.py registers three tools and
none creates a change set"*. `publish_semantic_model_change` is that door. The
danger of a second door is never that it is missing -- it is that it grows a second
implementation, and then two callers get two answers to "what does this metric
mean". So every test below is about the door being the SAME path, not about it
working.

THE FOUR PROPERTIES, IN THE ORDER THEY MATTER:

  1. the tool calls `create_change_set` -> `prepare_change_set` ->
     `confirm_change_set` of `core.semantic_model` -- proven by patching them and
     watching, never by reading the source;
  2. it holds NO second implementation: it imports nothing private from
     `core.semantic_model`, writes no SQL against the semantic tables, and the
     lifecycle names it calls are exactly those the REST door calls (derived from
     `semantic_model_api.py`, not restated here);
  3. a preparation that refuses PUBLISHES NOTHING -- confirm is never reached;
  4. the confirmation token `prepare` mints once never enters the tool's output.

NO POSTGRES. What is under test is composition and refusal, not the store; the
store has its own pg-gated tests (`test_semantic_model.py`). The connection is a
double so the nominal path is reached rather than fail-closed -- the same reasoning
`tests/isolation/test_mcp_tool_scope_refusal.py` writes for its own double.
"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

CORE = Path(__file__).resolve().parents[2] / "core"
DOOR = CORE / "governance_mcp.py"
REST_DOOR = CORE / "semantic_model_api.py"

TOOL_NAME = "publish_semantic_model_change"

#: A well-formed creation. `create_change_set` refuses a flat payload by name, so
#: nesting under `concept` is not decoration -- it is the shape the server reads.
INTENT = {
    "action": "create_concept",
    "concept": {
        "kind": "metric",
        "name": "clicks",
        "value_type": "integer",
        "expression": {"operation": "source_measure", "measure": "clicks"},
    },
}


# ---------------------------------------------------------------------------
# Harness -- capture the handler at registration, exactly like its neighbours.
# ---------------------------------------------------------------------------


def _tool(name: str = TOOL_NAME):
    """The handler `governance_mcp.register` registers, captured at registration.

    These tools are LOCAL functions of `register(mcp)`: there is no module
    attribute to call. The pattern is the neighbours' own
    (`test_mcp_tool_scope_refusal._tool_registered_by`), reused rather than
    reinvented.
    """
    import core.governance_mcp as module
    import core.mcp_profiles as profiles

    captured: dict[str, object] = {}
    declarations: dict[str, dict] = {}

    def _record(_mcp, handler, **kwargs):
        captured[handler.__name__] = handler
        declarations[handler.__name__] = kwargs
        return handler

    original = profiles.register_profiled
    profiles.register_profiled = _record
    try:
        module.register(object())
    finally:
        profiles.register_profiled = original
    return captured[name], declarations[name]


@contextmanager
def _a_connection_that_opens(_identity):
    yield MagicMock()


@pytest.fixture()
def a_holder(monkeypatch):
    """An identity that HOLDS the project, so the nominal path is reached."""
    token = SimpleNamespace(claims={"sub": "person_owner"}, client_id="cli")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token", lambda: token, raising=False
    )
    monkeypatch.setattr("core.db.request_connection", _a_connection_that_opens)
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(
            allowed=True, org_id="org_x", reason=None, capability="edit"
        ),
    )
    return token


def _prepared(*, publishable: bool, token: str = "tok_secret_value") -> dict:
    """What `prepare_change_set` returns, in its real shape."""
    refusals = (
        []
        if publishable
        else [{"code": "unknown_kind", "message": "A Concept is a metric or a dimension.",
               "path": "$.concept.kind"}]
    )
    return {
        "change_set_id": "scs_x",
        "state": "prepared",
        "diff": {"added": []},
        "validation": {
            "publishable": publishable,
            "refusals": refusals,
            "test_gate": {"state": "pass" if publishable else "unverifiable"},
            "used_by_impact": {"pinned": 0},
        },
        "refusals": refusals,
        "confirmation_token": token,
        "confirmation_expires_at": "2026-08-25T12:00:00+00:00",
    }


def _wire(monkeypatch, *, publishable: bool = True, token: str = "tok_secret_value"):
    """Patch the three lifecycle functions and record every call."""
    calls: list[tuple[str, dict]] = []

    def _create(_conn, project_id, **kwargs):
        calls.append(("create", {"project_id": project_id, **kwargs}))
        return SimpleNamespace(id="scs_x")

    def _prepare(_conn, project_id, change_set_id, **kwargs):
        calls.append(
            ("prepare", {"project_id": project_id, "change_set_id": change_set_id, **kwargs})
        )
        return _prepared(publishable=publishable, token=token)

    def _confirm(_conn, project_id, change_set_id, **kwargs):
        calls.append(
            ("confirm", {"project_id": project_id, "change_set_id": change_set_id, **kwargs})
        )
        return {
            "change_set": {"change_set_id": change_set_id, "state": "confirmed"},
            "result_version_id": "scv_1",
            "replayed": False,
        }

    monkeypatch.setattr("core.semantic_model.create_change_set", _create)
    monkeypatch.setattr("core.semantic_model.prepare_change_set", _prepare)
    monkeypatch.setattr("core.semantic_model.confirm_change_set", _confirm)
    return calls


# ---------------------------------------------------------------------------
# 1. The shared path -- patch the console's function, the tool calls it.
# ---------------------------------------------------------------------------


def test_the_tool_walks_the_console_lifecycle_in_order(a_holder, monkeypatch):
    """create -> prepare -> confirm, the three functions the REST door calls.

    Patching them is the only proof that counts: a module that re-derived the
    lifecycle would still be green against a source-reading assertion.
    """
    calls = _wire(monkeypatch)
    handler, _ = _tool()

    result = handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )

    assert [name for name, _ in calls] == ["create", "prepare", "confirm"], (
        "the door did not walk the change-set lifecycle it shares with the console: "
        f"{[name for name, _ in calls]}"
    )
    assert result.structured_content["data"]["published"] is True
    assert result.structured_content["data"]["result_version_id"] == "scv_1"


def test_the_actor_and_the_org_come_from_the_authorized_project(a_holder, monkeypatch):
    """A caller-supplied org is never authority -- the REST door's rule, held here.

    `confirm_change_set` writes the audit under `org_id`; taking it from the tool
    arguments would let a caller file its act under someone else's organization.
    """
    calls = _wire(monkeypatch)
    handler, _ = _tool()

    handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )

    confirm = dict(calls[2][1])
    assert confirm["org_id"] == "org_x", (
        "the organization must be derived from the AUTHORIZED project, exactly as "
        "`semantic_model_api._with_project` derives it"
    )
    assert confirm["actor"] == "person_owner"
    assert dict(calls[0][1])["actor"] == "person_owner"


def test_the_exact_base_travels_untouched(a_holder, monkeypatch):
    """An edit begins from an exact object and an exact base version.

    The door forwards both rather than resolving "the current version" itself:
    resolving it here would rebase the edit onto whatever became current, which is
    the refusal `create_change_set` exists to make.
    """
    calls = _wire(monkeypatch)
    handler, _ = _tool()

    handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent={"action": "edit_concept", "concept": {"kind": "metric", "name": "clicks"}},
        idempotency_key="idem_1",
        object_id="sc_1",
        base_version_id="scv_0",
    )

    created = dict(calls[0][1])
    assert created["object_id"] == "sc_1"
    assert created["base_version_id"] == "scv_0"


def test_a_written_override_reaches_prepare_in_the_shape_it_reads(a_holder, monkeypatch):
    """The Test gate override is a REASON, and `prepare_change_set` reads a mapping.

    `semantic_model_api._prepare` passes `payload.get("test_gate_override")`, whose
    `reason` the server validates at 20 characters. Passing a bare string here would
    make every override arrive unreadable and every publication refuse.
    """
    calls = _wire(monkeypatch)
    handler, _ = _tool()
    reason = "No Test verdict exists for a first Concept; publishing on record."

    handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
        test_gate_override_reason=reason,
    )

    assert dict(calls[1][1])["allow_test_override"] == {"reason": reason}


def test_no_override_is_sent_when_none_was_written(a_holder, monkeypatch):
    """A blank reason is not an override. `prepare_change_set` refuses a reason
    under 20 characters; sending `{"reason": ""}` would turn "I asked for nothing"
    into a named refusal about something the caller never requested."""
    calls = _wire(monkeypatch)
    handler, _ = _tool()

    handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
        test_gate_override_reason="   ",
    )

    assert dict(calls[1][1])["allow_test_override"] is None


# ---------------------------------------------------------------------------
# 2. No second implementation.
# ---------------------------------------------------------------------------


def _names_imported_from(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            found.update(alias.name for alias in node.names)
    return found


def test_the_door_imports_only_what_the_rest_door_imports():
    """The two doors reach `core.semantic_model` through the SAME public names.

    Derived from `semantic_model_api.py` rather than restated: a lifecycle function
    that moves, or a fourth one that appears, must break this line instead of
    letting one door quietly diverge from the other.
    """
    rest = _names_imported_from(REST_DOOR, "core.semantic_model")
    mcp = _names_imported_from(DOOR, "core.semantic_model")

    assert mcp <= rest, (
        "the MCP door reaches names of `core.semantic_model` that the console door "
        f"does not: {sorted(mcp - rest)}. A door that needs a name the other one "
        "never needed is composing its own lifecycle."
    )
    assert {"create_change_set", "prepare_change_set", "confirm_change_set"} <= mcp, (
        "the MCP door no longer imports the three lifecycle functions; whatever it "
        "calls instead is a second implementation."
    )


def test_the_door_holds_no_private_name_and_no_semantic_sql():
    """A second implementation announces itself two ways, and both are refused.

    A private name of `core.semantic_model` means the door reached past the
    published contract; a reference to `app.semantic_*` means it wrote its own
    query against tables it does not own.
    """
    source = DOOR.read_text(encoding="utf-8")
    private = sorted(
        name for name in _names_imported_from(DOOR, "core.semantic_model")
        if name.startswith("_")
    )
    assert not private, f"private names imported from core.semantic_model: {private}"

    #: The prose of this module names the tables it does NOT touch; only code
    #: lines are read, so a docstring may keep explaining them.
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    for table in ("app.semantic_change_sets", "app.semantic_concepts", "app.semantic_views"):
        assert table not in code, (
            f"`governance_mcp` names {table} in code: the change-set store belongs to "
            "`core.semantic_model`, and a query written here is the second "
            "implementation this door exists to avoid."
        )


def test_the_capability_rank_is_read_from_the_twin_door(a_holder, monkeypatch):
    """`edit`, and it is the rank `semantic_model_api` holds -- not a copy of it.

    Two ranks for one gesture would be two answers to "who may declare a metric".
    The assertion compares against `_WRITE_CAPABILITY` itself, so raising the REST
    door's rank without raising this one breaks here rather than in production.
    """
    from core.semantic_model_api import _WRITE_CAPABILITY

    _wire(monkeypatch)
    seen: list[str] = []

    def _record(*_a, **kwargs):
        seen.append(kwargs.get("minimum_capability", "view"))
        return SimpleNamespace(allowed=True, org_id="org_x", reason=None, capability="edit")

    monkeypatch.setattr("core.project_access.resolve_strict_resource_access", _record)
    handler, _ = _tool()
    handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )

    assert seen == [_WRITE_CAPABILITY]
    assert _WRITE_CAPABILITY == "edit", (
        "a viewer must not publish meaning, and publishing is not an administrative "
        "act either -- if this rank moved, both doors must move together"
    )


# ---------------------------------------------------------------------------
# 3. A refusal publishes nothing.
# ---------------------------------------------------------------------------


def test_a_refused_preparation_never_reaches_confirm(a_holder, monkeypatch):
    """`publishable is not True` stops the act. Nothing is published.

    This is the branch `NewConceptDialog` takes, and the reason it exists there:
    `prepare` mints a token whether or not it cleared the change set, so checking
    the token alone reads a refused change set as an accepted one.
    """
    calls = _wire(monkeypatch, publishable=False)
    handler, _ = _tool()

    result = handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )

    assert [name for name, _ in calls] == ["create", "prepare"], (
        "confirm was reached on a change set preparation refused"
    )
    data = result.structured_content["data"]
    assert data["published"] is False
    assert data["refusals"][0]["code"] == "unknown_kind"
    assert "Nothing was published" in result.content[0].text


def test_the_refusal_names_the_gesture_that_unblocks_the_test_gate(a_holder, monkeypatch):
    """An unverifiable gate is answered by a written override, and the text says so.

    A message that only reported the state would leave an agent retrying the same
    call: the gate can never become `pass` on its own for a candidate Test has no
    verdict on.
    """
    _wire(monkeypatch, publishable=False)
    handler, _ = _tool()

    result = handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )

    text = result.content[0].text
    assert "test_gate_override_reason" in text
    assert "idempotency_key" in text, (
        "the retry must resume the SAME change set; without naming the key, an agent "
        "opens a second one for the same act"
    )


def test_a_named_refusal_of_the_store_travels_with_its_code(a_holder, monkeypatch):
    """`SemanticRefused` carries a code, and the door does not flatten it.

    An agent told only "invalid" retries the same call. The code is what lets it
    change something.
    """
    from core.semantic_model import SemanticRefused

    def _boom(*_a, **_k):
        raise SemanticRefused("unknown_intent", "'rename' is not a governed action.")

    monkeypatch.setattr("core.semantic_model.create_change_set", _boom)
    handler, _ = _tool()

    with pytest.raises(ToolError) as excinfo:
        handler(
            project_id="proj_mine",
            object_type="semantic-concept",
            intent={"action": "rename"},
            idempotency_key="idem_1",
        )
    assert "unknown_intent" in str(excinfo.value)


def test_drift_is_not_reported_as_a_plain_refusal(a_holder, monkeypatch):
    """`SemanticStale` IS a `SemanticRefused`; caught in the wrong order it would
    arrive wearing the generic code, and an agent would retry an act the world has
    moved under instead of re-preparing it."""
    from core.semantic_model import SemanticStale

    _wire(monkeypatch)

    def _boom(*_a, **_k):
        raise SemanticStale("dependency_drift", "A pinned version changed.")

    monkeypatch.setattr("core.semantic_model.confirm_change_set", _boom)
    handler, _ = _tool()

    with pytest.raises(ToolError) as excinfo:
        handler(
            project_id="proj_mine",
            object_type="semantic-concept",
            intent=INTENT,
            idempotency_key="idem_1",
        )
    assert "dependency_drift" in str(excinfo.value)


def test_an_idempotency_key_is_required_before_anything_is_opened(a_holder, monkeypatch):
    """Without it a retried call opens a SECOND change set for one act."""
    calls = _wire(monkeypatch)
    handler, _ = _tool()

    with pytest.raises(ToolError) as excinfo:
        handler(
            project_id="proj_mine",
            object_type="semantic-concept",
            intent=INTENT,
            idempotency_key="",
        )
    assert "missing_idempotency_key" in str(excinfo.value)
    assert not calls


# ---------------------------------------------------------------------------
# 4. The confirmation token never enters model context.
# ---------------------------------------------------------------------------


def test_the_confirmation_token_is_used_and_never_returned(a_holder, monkeypatch):
    """Minted by prepare, consumed by confirm, absent from everything the model reads.

    The console round-trips this token through the browser; here the whole sequence
    happens server-side, so the token has no reason to leave. `review_agent_change`
    above holds the same posture for its own secret.
    """
    secret = "tok_UNIQUE_SENTINEL_VALUE"
    calls = _wire(monkeypatch, token=secret)
    handler, _ = _tool()

    result = handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )

    assert dict(calls[2][1])["confirmation_token"] == secret, (
        "confirm must consume the token prepare minted, not a second one"
    )
    rendered = json.dumps(result.structured_content) + result.content[0].text
    assert secret not in rendered, (
        "the single-use confirmation token reached model context; it is minted and "
        "consumed inside one call and has no reason to leave the server"
    )


def test_a_refused_preparation_does_not_leak_the_token_either(a_holder, monkeypatch):
    """The refusal path composes its own envelope, so it is its own risk."""
    secret = "tok_UNIQUE_SENTINEL_VALUE"
    _wire(monkeypatch, publishable=False, token=secret)
    handler, _ = _tool()

    result = handler(
        project_id="proj_mine",
        object_type="semantic-concept",
        intent=INTENT,
        idempotency_key="idem_1",
    )
    rendered = json.dumps(result.structured_content) + result.content[0].text
    assert secret not in rendered


# ---------------------------------------------------------------------------
# 5. The declaration matches the act.
# ---------------------------------------------------------------------------


def test_the_declaration_is_a_confirmed_write_a_human_authorizes():
    """A governed write declares `confirmed_write`, and `_assert_consistent` then
    forces `host` or `human`. `human` is the rank its two neighbours in this module
    hold: publishing a Concept decides what every render of the Project counts."""
    _, declaration = _tool()
    assert declaration["effect"] == "confirmed_write"
    assert declaration["confirmation_mode"] == "human"
    assert declaration["profile"] == "governance"


def test_the_tool_is_registered_on_the_real_catalogue():
    """It is not enough to be defined -- `register` must hand it to `register_profiled`,
    or the door exists in the file and nowhere on the wire.

    Against the REAL assembled catalogue: importing `core.main` boots the app
    without touching Postgres, exactly as `test_mcp_tool_surface.py` does, and
    `governance_mcp.register(mcp)` runs there or nowhere.
    """
    import core.mcp_profiles as profiles
    from core import main as core_main  # noqa: F401,PLC0415 -- the boot IS the test

    names = {declaration.name for declaration in profiles.registered_declarations()}
    assert TOOL_NAME in names, (
        f"{TOOL_NAME} is absent from the registered declarations: the module defines "
        "a door the catalogue never opened"
    )
