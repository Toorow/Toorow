"""AI-345: the render TOOL, driven in-process, accepts every family's normalized spec.

Measured 2026-09-01 by a real MCP session against the deployed server:
`render_analyze_result` refused its own pinned Visualization Spec with
`unsafe_render_payload` at `$.render_input.spec.document.bindings.series`. The
repair (AI-337, `render_app_payload._GOVERNED_WELL_NODE_PATH_SUFFIX`) was in the
repository and proven by a FUNCTION test; nothing drove the TOOL with a document
the grammar had normalized, and gate G14 composes its envelope itself, so it was
green while the tool refused.

What these tests hold the tool to, and it is the tool -- registered on FastMCP
and called through a client, never `compose_render_app_payload` on its own:

  * for EVERY family of `VISUAL_FAMILIES`, a document produced by
    `visualization_specs.normalize_document` with that family's own wells filled
    crosses the tool with no refusal, and the `_meta` payload carries the
    bindings unchanged -- `series` populated where the family declares it, the
    empty wells the grammar emits everywhere else;
  * the exemption is the well NAME only: an option object hidden under a well is
    still refused, through the tool, by the key it wears.

The seams below are the ones `test_mcp_analyze_app_payload.py` already patches;
they replace a PostgreSQL row read, never the composition the tool performs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from core import analyze_render_mcp as adapter
from core import feedback_review, mcp_profiles, render_app_payload, visualization_specs
from core.result_slices import RESULT_META_KEY
from core.visualization_families import VISUAL_FAMILIES, WELL_ROLES
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.anyio

HASH = "c" * 64
RESULT_ID = "qr_EXAMPLE"
QUERY_SPEC_VERSION_ID = "qsv_EXAMPLE"
SPEC_VERSION_ID = "vsv_EXAMPLE"
PROFILE = "mcp-inline"
RUNTIME_BUILD = "@toorow/card-shell/viz@0.1.0+abcdef0123456"
#: Wells whose members are numbers in a Result row; every other well is text.
NUMERIC_WELLS = frozenset({"measure", "size"})


def _member_id(well: str, index: int) -> str:
    return f"sc_EXAMPLE_{well.upper()}_{index}"


def _column(well: str, index: int) -> str:
    return f"{well}_{index}"


def _family_document(family) -> dict[str, Any]:
    """The proposal a Builder would send: every well the family declares, filled."""
    bindings = {well.name: [_member_id(well.name, 0)] for well in family.wells}
    first_well = family.wells[0].name
    return {
        "spec_contract_version": "visualization-spec.v1",
        "schema_version": 1,
        "family": family.id,
        "bindings": bindings,
        "order": {"source": "result"},
        "axes": {
            "x": {"scale": "categorical", "zero_baseline": True, "tick_density": "normal"},
            "y": {"scale": "linear", "zero_baseline": True, "tick_density": "normal"},
        },
        "legend": {"position": "right", "visible": True},
        "formatting": {
            "number_style": "auto",
            "date_style": "auto",
            "unit_source": "semantic_view",
        },
        "color": {"role": "categorical", "semantic_direction": "higher_is_better"},
        "interactions": {
            "hover": True,
            "select": True,
            "zoom": False,
            "legend_toggle": True,
            "local_filter": False,
        },
        "evidence": {"datum_fields": list(bindings[first_well]), "mark_binding": "datum"},
        "responsive": {"profiles": [PROFILE]},
        "accessibility": {"summary_source": "result_manifest", "table_fallback": "required"},
    }


def _result_for(family) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A schema naming every bound member, and two rows keyed by column name."""
    fields = [
        {"id": _member_id(well.name, 0), "name": _column(well.name, 0)} for well in family.wells
    ]
    rows = []
    for row_index in range(2):
        row: dict[str, Any] = {}
        for well in family.wells:
            column = _column(well.name, 0)
            row[column] = (
                (row_index + 1) * 10 if well.name in NUMERIC_WELLS else f"{column}-{row_index}"
            )
        rows.append(row)
    return {"fields": fields}, rows


def _write_runtime(tmp_path) -> str:
    """A manifest declaring a renderer for EVERY family, so no family is skipped."""
    bundle = tmp_path / "mcp-app.html"
    bundle.write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "runtime_build": RUNTIME_BUILD,
        "theme_version": "viz-theme@1",
        "formatter_version": "viz-formatters@1",
        "renderers": {
            family.id: {
                "renderer_build": f"{family.id}/toorow-echarts-{family.id}@1.0.0",
                "schema_versions": {"min": 1, "max": 1},
                "profiles": [PROFILE],
            }
            for family in VISUAL_FAMILIES
        },
    }
    bundle.with_name("runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(bundle)


def _wire_tool(monkeypatch, tmp_path, *, family, document: dict[str, Any]) -> FastMCP:
    """Register the real render tool with the PostgreSQL reads replaced by rows."""
    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", _write_runtime(tmp_path))
    schema, rows = _result_for(family)
    result_row = {
        "id": RESULT_ID,
        "query_spec_version_id": QUERY_SPEC_VERSION_ID,
        "outcome": "success",
        "content_hash": HASH,
        "row_count": len(rows),
        "truncated": False,
        "ai_path": "No AI path",
    }
    payload_row = {
        "content_hash": HASH,
        "result_schema": schema,
        "manifest": {"grain": "day"},
        "rows_chunk": rows,
    }
    result_meta = {
        "result_id": RESULT_ID,
        "content_hash": HASH,
        "projection_size": "small",
        "rows": rows,
        "row_count": len(rows),
    }

    def answer(project_id, result_id, tool_name, *, meta_finalizer):
        # `_answer` owns the transaction and the two row reads; the tool's own
        # finalizer -- the composition under test -- runs unchanged.
        finalized = meta_finalizer(
            conn=object(),
            identity="owner@example.com",
            org_id="org_EXAMPLE",
            result=result_row,
            payload=payload_row,
            meta={RESULT_META_KEY: result_meta},
        )
        return "answer", {"result_id": result_id}, finalized

    monkeypatch.setattr(adapter, "_answer", answer)
    monkeypatch.setattr(adapter, "_identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        feedback_review,
        "record_feedback_eligibility",
        lambda *_args, **_kwargs: {"schema_version": "feedback-eligibility.v1"},
    )
    monkeypatch.setattr(
        visualization_specs,
        "load_visualization_spec_version",
        lambda *args, **kwargs: {
            "id": SPEC_VERSION_ID,
            "query_spec_version_id": QUERY_SPEC_VERSION_ID,
            "spec_contract_version": document["spec_contract_version"],
            "schema_version": document["schema_version"],
            "family": document["family"],
            "spec": document,
        },
    )
    target = FastMCP("ai-345-render-tool")
    adapter.register(target)
    return target


async def _call_render_tool(target: FastMCP):
    async with Client(FastMCPTransport(target)) as client:
        return await client.call_tool(
            mcp_profiles.RENDER_TOOL_NAME,
            {
                "project_id": "proj_EXAMPLE",
                "result_id": RESULT_ID,
                "visualization_spec_version_id": SPEC_VERSION_ID,
            },
        )


@pytest.mark.parametrize("family", VISUAL_FAMILIES, ids=[f.id for f in VISUAL_FAMILIES])
async def test_the_render_tool_accepts_every_family_normalized_by_the_grammar(
    monkeypatch, tmp_path, family
):
    normalized, refusals = visualization_specs.normalize_document(_family_document(family))
    assert refusals == [], [refusal.__dict__ for refusal in refusals]
    # The grammar emits EVERY well on every document; that is what the guard meets.
    assert set(normalized["bindings"]) == set(WELL_ROLES)
    declared = {well.name for well in family.wells}
    assert all(bool(normalized["bindings"][well]) == (well in declared) for well in WELL_ROLES)

    target = _wire_tool(monkeypatch, tmp_path, family=family, document=normalized)
    result = await _call_render_tool(target)

    assert result.is_error is False
    payload = result.meta[render_app_payload.APP_PAYLOAD_META_KEY]
    render_app_payload.validate_render_app_payload(payload)
    document = payload["render_input"]["spec"]["document"]
    assert document["bindings"] == normalized["bindings"]
    assert payload["render_input"]["pins"]["renderer_build"] == (
        f"{family.id}/toorow-echarts-{family.id}@1.0.0"
    )
    if "series" in declared:
        assert document["bindings"]["series"] == [_member_id("series", 0)]


async def test_the_render_tool_still_refuses_an_option_object_hidden_under_a_well(
    monkeypatch, tmp_path
):
    """The exemption is the well NAME; its value is walked, through the tool too."""
    family = next(f for f in VISUAL_FAMILIES if any(w.name == "series" for w in f.wells))
    normalized, refusals = visualization_specs.normalize_document(_family_document(family))
    assert refusals == []
    # Past the grammar on purpose: this is what a stored document could only carry
    # if the grammar were bypassed, and the guard must not rely on the grammar.
    normalized["bindings"]["series"] = [{"renderer_options": {"xaxis": {}}}]

    target = _wire_tool(monkeypatch, tmp_path, family=family, document=normalized)
    with pytest.raises(ToolError) as excinfo:
        await _call_render_tool(target)
    assert "unsafe_render_payload" in str(excinfo.value)
    assert "renderer_options" in str(excinfo.value)
