"""Story 65.1: a real FastMCP CallToolResult carries the CAPTURED render wire.

AI-337, 2026-08-31: this file used to import `ROWS`, `RESULT_META` and
`SPEC_DOCUMENT` from the generator -- literals the generator AUTHORED -- and
assert the render tool reproduced them. The generator no longer authors anything:
it captures one execution against the local warehouse. So the seams below are fed
from the COMMITTED CAPTURE itself, and what the test proves is what it always
meant to prove -- the real tool, given that exact frozen Result and that exact
Visualization Spec version, still composes byte for byte the wire in the
repository. The Result identities, rows and schema are the executed ones; nothing
here types one.

Re-capture (a real PostgreSQL and the local DuckDB warehouse, both listed in the
generator's docstring):

    uv run python scripts/generate_analyze_render_tool_result_fixture.py
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from core import analyze_render_mcp as adapter
from core import (
    feedback_review,
    mcp_profiles,
    query_specs_api,
    render_app_payload,
    visualization_specs,
)
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport

_GENERATOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts/generate_analyze_render_tool_result_fixture.py"
)
_SPEC = importlib.util.spec_from_file_location("analyze_render_fixture_generator", _GENERATOR_PATH)
assert _SPEC and _SPEC.loader
_GENERATOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GENERATOR)

CAPTURE = json.loads(_GENERATOR.FIXTURE_PATH.read_text(encoding="utf-8"))
RENDER_INPUT = CAPTURE["_meta"]["toorow.app_payload"]["render_input"]
RESULT_META = CAPTURE["_meta"]["toorow.result"]
CAPTURED_RESULT = RENDER_INPUT["result"]
CAPTURED_SPEC = RENDER_INPUT["spec"]
ROWS = CAPTURED_RESULT["rows"]
SPEC_DOCUMENT = CAPTURED_SPEC["document"]
PINS = RENDER_INPUT["pins"]

#: The Query Spec version the two stubs must AGREE on. It never reaches the wire
#: -- the render tool only compares the Result's to the Spec's and refuses a
#: mismatch -- so the capture does not carry it and nothing here can read it back.
QUERY_SPEC_VERSION_ID = "qsv_REPLAY"

CAPTURED_TEXT = "".join(
    block["text"] for block in CAPTURE["content"] if block.get("type") == "text"
)
CAPTURED_SUMMARY = CAPTURE["structuredContent"]

pytestmark = pytest.mark.anyio


def _write_runtime(tmp_path):
    bundle = tmp_path / "mcp-app.html"
    bundle.write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "runtime_build": PINS["runtime_build"],
        "theme_version": PINS["theme_version"],
        "formatter_version": PINS["formatter_version"],
        "renderers": {
            SPEC_DOCUMENT["family"]: {
                "renderer_build": PINS["renderer_build"],
                "schema_versions": {"min": 1, "max": 1},
                "profiles": ["mcp-inline"],
            }
        },
    }
    bundle.with_name("runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


async def test_real_render_tool_result_matches_the_generated_wire(monkeypatch):
    runtime_dir = Path(__file__).resolve().parents[3] / ".codex-tmp/mcp-render-fixture"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    bundle = _write_runtime(runtime_dir)
    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", str(bundle))
    # The frozen Result the capture drew, replayed exactly. `manifest` is the
    # PROJECTED one the envelope carries; the render path projects again and the
    # projection is idempotent, which the byte-for-byte comparison below proves.
    result_row = {
        "id": CAPTURED_RESULT["result_id"],
        "query_spec_version_id": QUERY_SPEC_VERSION_ID,
        "outcome": CAPTURED_RESULT["outcome"],
        "content_hash": CAPTURED_RESULT["content_hash"],
        "row_count": CAPTURED_RESULT["row_count"],
        "truncated": CAPTURED_RESULT["truncated"],
        "ai_path": "No AI path",
    }
    payload_row = {
        "content_hash": CAPTURED_RESULT["content_hash"],
        "result_schema": CAPTURED_RESULT["schema"],
        "manifest": CAPTURED_RESULT["manifest"],
        "rows_chunk": ROWS,
    }

    def answer(*args, **kwargs):
        finalized = kwargs["meta_finalizer"](
            conn=object(),
            identity="owner@example.com",
            org_id="org_FIXTURE",
            result=result_row,
            payload=payload_row,
            meta={"toorow.result": RESULT_META},
        )
        # The two READ channels are replayed from the capture rather than
        # re-composed: `_answer` is stubbed here, so composing them would only
        # test the stub. They are proven against a real PostgreSQL by
        # `tests/integration/test_analyze_result_slices_pg.py`. What this test
        # owns is the third channel -- `_meta`, built above by the tool's own
        # finalizer from the captured Result and Visualization Spec.
        return CAPTURED_TEXT, CAPTURED_SUMMARY, finalized

    monkeypatch.setattr(
        adapter,
        "_answer",
        answer,
    )
    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        feedback_review,
        "record_feedback_eligibility",
        lambda *_args, **_kwargs: {"schema_version": "feedback-eligibility.v1"},
    )

    class Decision:
        org_id = "org_FIXTURE"

    monkeypatch.setattr(adapter, "_guard_project_view", lambda *args: Decision())

    @contextlib.contextmanager
    def connection(identity):
        yield object()

    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(
        adapter,
        "_load_result",
        lambda *args, **kwargs: result_row,
    )
    monkeypatch.setattr(
        adapter,
        "_load_payload",
        lambda *args, **kwargs: payload_row,
    )
    monkeypatch.setattr(
        visualization_specs,
        "load_visualization_spec_version",
        lambda *args, **kwargs: {
            "id": CAPTURED_SPEC["visualization_spec_version_id"],
            "query_spec_version_id": QUERY_SPEC_VERSION_ID,
            "spec_contract_version": CAPTURED_SPEC["spec_contract_version"],
            "schema_version": CAPTURED_SPEC["schema_version"],
            "family": SPEC_DOCUMENT["family"],
            "spec": SPEC_DOCUMENT,
        },
    )

    target = FastMCP("story-65-1")
    adapter.register(target)
    async with Client(FastMCPTransport(target)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        result = await client.call_tool(
            mcp_profiles.RENDER_TOOL_NAME,
            {
                "project_id": "proj_FIXTURE",
                "result_id": CAPTURED_RESULT["result_id"],
                "visualization_spec_version_id": CAPTURED_SPEC[
                    "visualization_spec_version_id"
                ],
            },
        )

    assert tools[mcp_profiles.RENDER_TOOL_NAME].meta["ui"]["resourceUri"] == (
        "ui://core/visualization-runtime"
    )
    #  THE PRESENTATION IS NAMED, AND IT MAY BE NAMED TWO WAYS (story 72.7).
    #  `visualization_spec_version_id` was a REQUIRED parameter until the Chart
    #  Template door opened beside it; a schema that still required it would make
    #  the alternative uncallable. So the schema requires the Result identity, it
    #  OFFERS both pins, and "exactly one of them" is enforced where a JSON Schema
    #  derived from a signature cannot say it: at the call, by two named refusals
    #  (`visualization_pin_ambiguous` for both, `missing_param` for neither), each
    #  proved in `server/tests/core/test_analyze_template_offer.py`.
    schema = tools[mcp_profiles.RENDER_TOOL_NAME].inputSchema
    assert schema["required"] == ["project_id", "result_id"]
    assert "visualization_spec_version_id" in schema["properties"]
    assert "visualization_template_version_id" in schema["properties"]
    actual = {
        "content": [block.model_dump(exclude_none=True) for block in result.content],
        "structuredContent": result.structured_content,
        "_meta": _GENERATOR.normalize_meta(result.meta),
        "isError": result.is_error,
    }
    assert actual == json.loads(_GENERATOR.FIXTURE_PATH.read_text(encoding="utf-8"))
    render_app_payload.validate_render_app_payload(actual["_meta"]["toorow.app_payload"])
