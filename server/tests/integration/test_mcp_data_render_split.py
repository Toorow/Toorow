"""Story 50.6 -- the data/render tool split, proved against the LIVE registry.

WHY THIS FILE IS DRIVEN OFF THE REGISTRY AND NOT OFF A LIST OF NAMES.
`visualization-and-rendering.md:344` names the downstream conflict this story
closes: *"Current server tools and delivery contracts can expose oversized
model-visible payloads"*. A story that edits four call sites leaves the fifth to
be written next week. So every assertion here iterates `tools/list` through the
real MCP layer -- the seam `test_cards_integration_seams.py` already uses -- and a
tool registered tomorrow is inside the invariant by construction.

The suite pairs a TRIPWIRE with a CONSTRUCTION. The secret-shape denylist is the
tripwire; `test_the_result_meta_key_has_exactly_one_builder` is the construction
that gives it meaning. A denylist alone is not a proof of absence.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from core.analyze_render_mcp import (
    REGISTRATION_STATE,
    RENDER_TOOL_ABSENT_REASON,
    RENDER_TOOL_OWNING_STORY,
)
from core.main import mcp
from core.mcp_profiles import (
    RENDER_TOOL_NAME,
    CatalogValidationError,
    app_only_tool_names,
    assert_data_render_split,
)
from core.skill_tool_catalog import list_skill_tool_catalog
from core.visualization_runtime_resource import VISUALIZATION_RUNTIME_URI
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from tests.conftest import SERVER_ROOT

pytestmark = pytest.mark.anyio


async def _wire_tools():
    """The MODEL's catalog: what `tools/list` returns after the middleware."""
    async with Client(FastMCPTransport(mcp)) as client:
        return await client.list_tools()


async def _assembled_tools():
    """Every registered tool, before any channel filter -- the app's population too.

    67.7 split the two: an app-only tool is registered and callable by a widget,
    and absent from `_wire_tools()` by design. A test that wants to read a
    declaration must read it here.
    """
    return await mcp._list_tools()


def _string_constants(path: Path) -> set[str]:
    """Every string LITERAL in real code -- docstrings and comments excluded.

    These assertions inspect what the module DOES, not what it says. Several
    modules name the retired construct on purpose, to record why it is gone; a
    prose mention is documentation, a live literal is the defect. A grep-based
    version of this test failed on its own explanatory comments, which is a good
    reminder that a text search is not a source assertion.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def _dict_keys(path: Path) -> set[str]:
    """Every literal key written into a dict literal in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def _widget_uri(tool) -> str | None:
    meta = tool.meta or {}
    ui = meta.get("ui") or {}
    uri = ui.get("resourceUri")
    return uri if isinstance(uri, str) and uri else None


# ---------------------------------------------------------------------------
# AC3 -- at most one tool advertises a widget, and it is the render tool.
# ---------------------------------------------------------------------------


async def test_data_tools_advertise_no_widget_resource():
    tools = await _wire_tools()
    assert tools, "the catalog must not be empty, or this proves nothing"
    bound = {t.name: _widget_uri(t) for t in tools if _widget_uri(t)}
    assert set(bound) <= {RENDER_TOOL_NAME}, (
        f"these tools advertise a widget resource and are not the render tool: "
        f"{sorted(set(bound) - {RENDER_TOOL_NAME})}"
    )
    if REGISTRATION_STATE["render_tool_registered"]:
        # The literal, not a variable that happens to look like it: if Story 50.5
        # lands a different URI this fails loudly instead of binding to nothing.
        assert bound[RENDER_TOOL_NAME] == "ui://core/visualization-runtime"
        assert bound[RENDER_TOOL_NAME] == VISUALIZATION_RUNTIME_URI


async def test_the_three_retired_bindings_are_closed_by_name():
    """The exact tools the story inventoried. Named, so the closure is checkable."""
    tools = {t.name: t for t in await _wire_tools()}
    for name in ("get_daily_report", "get_report", "get_card"):
        assert name in tools, f"{name} must stay registered -- only its binding is retired"
        assert _widget_uri(tools[name]) is None, f"{name} still advertises a widget"


async def test_no_model_visible_payload_advertises_an_app_resource():
    """`app_resource` inside `structuredContent` was the wrong channel twice over."""
    offenders = [
        p.name for p in (SERVER_ROOT / "core").glob("*.py") if "app_resource" in _dict_keys(p)
    ]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# AC8 -- app-only tools are not model-callable analytical tools.
# ---------------------------------------------------------------------------


async def test_app_only_tools_declare_app_visibility():
    tools = {t.name: t for t in await _assembled_tools()}
    for name in (
        "app_read_result_manifest",
        "app_read_result_slice",
        "submit_analyze_feedback",
    ):
        assert name in tools, f"{name} must be registered"
        assert (tools[name].meta or {}).get("ui", {}).get("visibility") == ["app"], (
            f"{name} wire meta.ui: {(tools[name].meta or {}).get('ui')}"
        )
    assert {
        "app_read_result_manifest",
        "app_read_result_slice",
        "submit_analyze_feedback",
    } <= app_only_tool_names()


async def test_app_only_tools_absent_from_skill_catalog():
    """A governed Skill must not be authorable against a slice reader."""
    catalog = await list_skill_tool_catalog()
    names = {t["name"] for t in catalog["tools"]}
    assert "app_read_result_manifest" not in names
    assert "app_read_result_slice" not in names
    assert "submit_analyze_feedback" not in names
    # The data tool IS an analytical capability and must stay authorable.
    assert "analyze_result" in names


async def test_the_data_tool_is_not_app_only():
    tools = {t.name: t for t in await _wire_tools()}
    for name in ("analyze_result", "execute_analyze_query_spec"):
        assert name in tools
        assert (tools[name].meta or {}).get("ui") is None


# ---------------------------------------------------------------------------
# AC5 -- the tripwire, AND the construction that gives it meaning.
# ---------------------------------------------------------------------------

_SECRET_KEY_NAMES = {
    "token",
    "access_token",
    "refresh_token",
    "bearer",
    "authorization",
    "password",
    "secret",
    "api_key",
    "client_secret",
    "dsn",
    "connection_string",
    "private_key",
    "credential",
}

_SECRET_VALUE_SHAPES = (
    re.compile(r"^ey[A-Za-z0-9_-]{10,}\."),  # JWT-shaped
    re.compile(r"postgres(ql)?://"),
    re.compile(r"Bearer "),
    re.compile(r"-----BEGIN"),
)


def _walk(value, path="_meta"):
    if isinstance(value, dict):
        for key, child in value.items():
            assert str(key).lower() not in _SECRET_KEY_NAMES, f"secret-shaped key at {path}.{key}"
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _walk(child, f"{path}[{i}]")
    elif isinstance(value, str):
        for shape in _SECRET_VALUE_SHAPES:
            assert not shape.search(value), f"secret-shaped value at {path}: {value[:40]!r}"
        yield path


async def test_meta_carries_no_secret_shaped_value():
    """The TRIPWIRE. It catches a mistake inside the one allowed builder."""
    from core.result_slices import RESULT_META_KEY, build_result_meta

    meta = build_result_meta(
        result_id="qr_EXAMPLE",
        content_hash="a" * 64,
        result_schema={"fields": [{"name": "day"}, {"name": "clicks"}]},
        manifest={"note": "manifest", "missing_link": None},
        rows_chunk=[{"day": "2026-07-01", "clicks": 1}],
        allowed_columns=["day", "clicks"],
        row_count=1,
        truncated=False,
        result_handle="rh_" + "0" * 26,
    )
    list(_walk(meta))
    # No warehouse relation name, no SQL, no internal person identifier can be in
    # there, because none of them is a parameter of the builder.
    serialized = json.dumps(meta[RESULT_META_KEY])
    assert "SELECT" not in serialized.upper()
    assert "@" not in serialized


async def test_the_result_meta_key_has_exactly_one_builder():
    """The CONSTRUCTION. A denylist is not a proof of absence; this is why it works.

    If a second module could write `toorow.result`, the tripwire above would guard
    one construction site out of two, and would be worth exactly nothing.
    """
    writers = sorted(
        p.name
        for p in (SERVER_ROOT / "core").glob("*.py")
        if "toorow.result" in _string_constants(p)
    )
    assert writers == ["result_slices.py"], writers


# ---------------------------------------------------------------------------
# AC4 -- the budget applies to the whole catalog, not to four remembered tools.
# ---------------------------------------------------------------------------


def _capability_middleware():
    """The REAL middleware instance the assembled server runs, or fail the test.

    Not a fresh one built for the occasion: `core.main` builds it once at import
    and attaches it with `mcp.add_middleware`. Reaching into the server's own
    middleware list is what makes this a test of the shipped configuration rather
    than of a class that happens to exist.
    """
    from core.mcp_profiles import build_middleware

    reference = type(build_middleware())
    installed = [
        mw
        for mw in (getattr(mcp, "middleware", None) or [])
        # The class is defined inside `build_middleware`, so every call mints a
        # fresh one and `is` never holds. Module + qualname identify the factory
        # that produced it, which is the fact worth asserting: the middleware the
        # server runs comes from `core.mcp_profiles`, not from something else
        # wearing the same class name.
        if type(mw).__module__ == reference.__module__
        and type(mw).__qualname__ == reference.__qualname__
    ]
    assert installed, (
        "CapabilityProfileMiddleware is not attached to the assembled server, so "
        "no tool is reached by the model-channel budget"
    )
    return installed[0]


async def test_every_data_tool_respects_the_model_channel_budget():
    """Every tool in the ASSEMBLED catalog is reached by the one shared guard.

    The previous version of this test asserted that two strings appeared somewhere
    in two files. It passed unchanged while `list_card_templates` shipped a
    7255-byte `structuredContent` against a 4096-byte budget, and it would have
    passed for a sixteenth unguarded tool -- because the budget was each tool's
    discipline at four remembered call sites, and a source grep cannot tell a
    guarded return from an unguarded one.

    So this drives the REAL `on_call_tool` hook, once per tool in
    `assembled_tools(mcp)`, with a deliberately over-budget result and a
    `call_next` that returns it. Every tool must refuse. That goes red if the hook
    is deleted, if the middleware stops being attached, or if a tool is registered
    on a server the middleware does not cover.
    """
    from core.mcp_profiles import assembled_tools
    from core.model_channel import MODEL_CHANNEL_MAX_BYTES, serialized_bytes
    from fastmcp.exceptions import ToolError
    from fastmcp.tools.tool import ToolResult

    middleware = _capability_middleware()
    tools = assembled_tools(mcp)
    assert len(tools) > 40, f"the assembled catalog looks wrong: {len(tools)} tools"

    # An envelope with nothing movable: one string far past the budget, under a key
    # the split may not route away. Nothing here can be quietly relocated, so the
    # only compliant outcome is a refusal.
    over_budget = {"schema_version": 1, "message": "x" * (MODEL_CHANNEL_MAX_BYTES + 1)}
    assert serialized_bytes(over_budget) > MODEL_CHANNEL_MAX_BYTES

    class _Message:
        def __init__(self, name):
            self.name = name
            self.arguments = {}

    class _Context:
        def __init__(self, name):
            self.message = _Message(name)

    async def _call_next(_context):
        return ToolResult(content="over budget", structured_content=over_budget)

    refused, denied_first, unguarded = [], [], []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str):
            continue
        try:
            await middleware.on_call_tool(_Context(name), _call_next)
        except ToolError as exc:
            body = json.loads(str(exc))
            if body.get("code") == "payload_over_budget" and body.get("tool") == name:
                refused.append(name)
            elif body.get("code") == "not_found":
                # Denied by the capability profile BEFORE the tool could return.
                # Counted separately and never as a pass: this test cannot observe
                # the budget for these, and calling that "covered" would be exactly
                # the guard-that-cannot-fail this epic keeps finding.
                denied_first.append(name)
            else:
                unguarded.append((name, body))
        else:
            unguarded.append((name, "returned an over-budget payload"))

    assert not unguarded, f"tools not reached by the model-channel budget: {unguarded}"
    assert refused, "no tool was observed refusing; the hook may not be running at all"
    # Every tool is accounted for by one of the two outcomes -- none simply passed.
    assert len(refused) + len(denied_first) == len(tools), (
        f"{len(tools)} tools, {len(refused)} refused, {len(denied_first)} denied first"
    )
    # The high-risk tools are denied here because this transport is anonymous, so
    # the behavioural half above cannot speak for them. The structural half below
    # is what covers them.
    for name in denied_first:
        assert name not in refused


async def test_the_budget_call_is_unconditional_in_the_hook():
    """The structural half: the budget is not reached "for the tools we remembered".

    The behavioural test above can only observe the tools this anonymous transport
    is allowed to call; the rest are denied by the capability profile before they
    return. So what covers THEM is that `enforce_result_model_channel` is called
    from the top level of `on_call_tool`'s body -- not inside an `if`, not filtered
    by `_REGISTRY.declarations` the way the visibility check immediately above it
    is, not restricted by tool name.

    That distinction is the whole finding. The visibility check IS name-filtered,
    and copying its shape onto the budget would silently exempt every tool
    registered on plain `mcp.tool` -- which is exactly the set that was over budget
    (`list_card_templates`, `get_daily_report`, `get_report`, `get_card`).
    """
    tree = ast.parse((SERVER_ROOT / "core/mcp_profiles.py").read_text(encoding="utf-8"))
    hooks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_call_tool"
    ]
    assert len(hooks) == 1, "expected exactly one on_call_tool hook"

    def _calls(nodes):
        return {
            n.func.id
            for stmt in nodes
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    top_level_calls = _calls(hooks[0].body)
    assert "enforce_result_model_channel" in top_level_calls

    # And not ONLY at the top level -- nowhere else. If the only call site were
    # inside a branch, the set above would still contain it via `ast.walk`, so
    # assert directly that no conditional encloses it.
    for node in ast.walk(hooks[0]):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
            assert "enforce_result_model_channel" not in _calls(
                list(node.body) + list(getattr(node, "orelse", []))
            ), "the model-channel budget became conditional inside on_call_tool"


async def test_the_budget_refusal_names_the_tool_the_measurement_and_the_budget():
    """AC4: it refuses rather than truncating, and the refusal is actionable."""
    from core.model_channel import MODEL_CHANNEL_MAX_BYTES
    from fastmcp.exceptions import ToolError
    from fastmcp.tools.tool import ToolResult

    middleware = _capability_middleware()

    class _Message:
        name = "list_card_templates"
        arguments: dict = {}

    class _Context:
        message = _Message()

    async def _call_next(_context):
        return ToolResult(
            content="x",
            structured_content={
                "schema_version": 1,
                "message": "y" * (MODEL_CHANNEL_MAX_BYTES + 1),
            },
        )

    with pytest.raises(ToolError) as excinfo:
        await middleware.on_call_tool(_Context(), _call_next)
    body = json.loads(str(excinfo.value))
    assert body["code"] == "payload_over_budget"
    assert body["tool"] == "list_card_templates"
    assert body["budget"] == MODEL_CHANNEL_MAX_BYTES
    assert body["measured"] > MODEL_CHANNEL_MAX_BYTES


def test_model_channel_moves_are_nested_without_overwriting_a_typed_render_payload():
    from core import render_app_payload
    from core.mcp_profiles import enforce_result_model_channel
    from core.model_channel import APP_PAYLOAD_META_KEY
    from fastmcp.tools.tool import ToolResult

    render_input = render_app_payload.compose_render_input(
        result={
            "result_id": "qr_EXAMPLE",
            "content_hash": "a" * 64,
            "outcome": "success",
            "schema": {"fields": [{"name": "value"}]},
            "rows": [{"value": 1}],
            "manifest": {},
        },
        spec={
            "visualization_spec_version_id": "vsv_EXAMPLE",
            "spec_contract_version": "visualization-spec.v1",
            "schema_version": 1,
            "document": {},
        },
        pins={
            "theme_version": "theme@1",
            "formatter_version": "formatter@1",
            "renderer_build": "bar@1",
            "runtime_build": "runtime@1",
        },
        profile="mcp-inline",
    )
    typed = render_app_payload.compose_render_app_payload(render_input=render_input)
    result = ToolResult(
        content="bounded",
        structured_content={
            "schema_version": 1,
            "data": {"rows": [{"value": "x" * 1000} for _ in range(10)]},
        },
        meta={APP_PAYLOAD_META_KEY: typed, "toorow.result": {"result_id": "qr_EXAMPLE"}},
    )
    bounded = enforce_result_model_channel("render_analyze_result", result)
    payload = bounded.meta[APP_PAYLOAD_META_KEY]
    assert payload["schema_version"] == 1
    assert payload["kind"] == "render"
    assert payload["render_input"] == typed["render_input"]
    assert payload["moved"]["rows"] == result.structured_content["data"]["rows"]
    assert bounded.meta["toorow.result"] == {"result_id": "qr_EXAMPLE"}


async def test_no_tool_in_main_still_carries_its_own_budget_call():
    """AC4 is "ONE shared guard", not "one guard plus four habits".

    Four per-site calls is how the catalog ended up 4 guarded / 15 unguarded in
    this one file. If a site starts guarding itself again, the class of defect is
    back even when that particular site is correct.
    """
    main_source = (SERVER_ROOT / "core/main.py").read_text(encoding="utf-8")
    for needle in (
        "_model_channel.enforce_model_channel",
        "_model_channel.partition_envelope",
        "_model_channel.app_meta",
    ):
        assert needle not in main_source, f"a per-site budget call returned: {needle}"
    # And every ToolResult built in main.py that carries `meta=` builds it through
    # the split, never by hand -- a hand-built `meta` is how a widget binding
    # returns.
    main_tree = ast.parse(main_source)
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Dict):
            literal_keys = {
                k.value for k in node.keys if isinstance(k, ast.Constant)
            }
            assert literal_keys != {"ui"}, (
                "a hand-built {'ui': ...} result meta reappeared in main.py"
            )


async def test_a_real_over_budget_tool_is_bounded_end_to_end():
    """`list_card_templates` was the shipped casualty. Measure it on the wire.

    It built a 7255-byte `structuredContent` against a 4096-byte budget and was in
    the assembled catalog. This calls it through the real MCP layer and asserts
    both halves of the repair: it is inside the budget now, and it is a PROJECTION
    rather than a truncation -- every registered template still appears, with the
    count stated, and the renderer layout and the app resource URI are gone from
    the model channel.
    """
    from core import cards as _cards
    from core.model_channel import MODEL_CHANNEL_MAX_BYTES, serialized_bytes

    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_card_templates", {})

    assert not result.is_error, result
    envelope = result.structured_content
    measured = serialized_bytes(envelope)
    assert measured <= MODEL_CHANNEL_MAX_BYTES, measured

    data = envelope["data"]
    assert isinstance(data["templates"], list), (
        "the catalog was routed WHOLE to the app channel instead of being projected, "
        "so the model that reads this tool to choose a card now sees a descriptor: "
        f"{data['templates']}"
    )
    registered = {t["id"] for t in _cards.list_templates()}
    assert {t["id"] for t in data["templates"]} == registered
    assert data["templates_total"] == len(registered)
    assert data["templates_withheld"] == 0
    serialized = json.dumps(envelope)
    assert "composition" not in serialized, "renderer layout in the model channel"
    assert "widget_uri" not in serialized and "ui://" not in serialized


# ---------------------------------------------------------------------------
# AC12 -- the invariant aborts boot, and the render tool's state is declared.
# ---------------------------------------------------------------------------


async def test_a_misdeclared_tool_aborts_boot():
    """The difference between a policy and a closed contract."""
    from fastmcp import FastMCP
    from fastmcp.apps import AppConfig

    rogue = FastMCP("rogue")

    @rogue.tool(app=AppConfig(resource_uri="ui://core/card-kpi"))
    def a_data_tool_sneaking_a_widget(project_id: str) -> str:
        """A data tool that advertises a widget."""
        return project_id

    with pytest.raises(CatalogValidationError) as excinfo:
        assert_data_render_split(rogue)
    assert "a_data_tool_sneaking_a_widget" in str(excinfo.value)
    assert RENDER_TOOL_NAME in str(excinfo.value)


async def test_register_profiled_refuses_a_widget_on_a_non_render_tool():
    """Fail closed at the REGISTRATION site, so the traceback names the offender."""
    from core.mcp_profiles import register_profiled
    from fastmcp import FastMCP
    from fastmcp.apps import AppConfig

    target = FastMCP("target")

    def some_data_tool(project_id: str) -> str:
        """Doc."""
        return project_id

    with pytest.raises(CatalogValidationError) as excinfo:
        register_profiled(
            target, some_data_tool,
            profile="insights", effect="read",
            data_class="operational", confirmation_mode="none",
            app=AppConfig(resource_uri="ui://core/daily-report"),
        )
    assert "some_data_tool" in str(excinfo.value)


async def test_register_profiled_refuses_a_hidden_mutating_tool():
    """An app-only tool that mutates is a side channel with an unusable ceremony."""
    from core.mcp_profiles import register_profiled
    from fastmcp import FastMCP
    from fastmcp.apps import AppConfig

    target = FastMCP("target")

    def a_hidden_writer(project_id: str) -> str:
        """Doc."""
        return project_id

    with pytest.raises(CatalogValidationError) as excinfo:
        register_profiled(
            target, a_hidden_writer,
            profile="operations", effect="confirmed_write",
            data_class="operational", confirmation_mode="human",
            app=AppConfig(visibility=["app"]),
        )
    assert "a_hidden_writer" in str(excinfo.value)


async def test_render_tool_presence_or_absence_is_declared_by_name_and_reason():
    """Half B must never become invisible: registered, or absent WITH its reason."""
    tools = {t.name for t in await _wire_tools()}
    if REGISTRATION_STATE["render_tool_registered"]:
        assert RENDER_TOOL_NAME in tools
        assert REGISTRATION_STATE["render_tool_resource_uri"] == VISUALIZATION_RUNTIME_URI
        assert REGISTRATION_STATE["render_tool_absent_reason"] is None
        # It is never bound to a retired connector-shaped widget as a stand-in.
        assert "card-" not in REGISTRATION_STATE["render_tool_resource_uri"]
        assert "daily-report" not in REGISTRATION_STATE["render_tool_resource_uri"]
    else:
        assert RENDER_TOOL_NAME not in tools
        assert REGISTRATION_STATE["render_tool_absent_reason"] == RENDER_TOOL_ABSENT_REASON
        assert REGISTRATION_STATE["render_tool_owning_story"] == RENDER_TOOL_OWNING_STORY


async def test_the_ui_core_resources_stay_registered_as_inventory():
    """CLAUDE.md anti-drift rule 3: destroying the inventory of a gap hides the gap.

    A resource no data tool advertises is inert. Deleting the registrations would
    erase the record of what Story 50.5 replaces.
    """
    async with Client(FastMCPTransport(mcp)) as client:
        resources = await client.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "ui://core/daily-report" in uris
    assert VISUALIZATION_RUNTIME_URI in uris
