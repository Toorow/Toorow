"""Story 8.7 — the MCP flow interface: list / get / validate / upsert.

These four tools are the LLM-agent channel of the COMMON flow interface;
``core.flows_api`` mirrors them over REST calling the SAME ``core.flows``
functions, so the two surfaces cannot drift. All heavy logic lives in
``core.flows`` — the bodies here only resolve identity and project, open a
connection, and return the dual-channel result (AD-1 envelope + lean text).

WHY THE HELPERS ARE IMPORTED INSIDE THE FUNCTIONS. ``_envelope``, ``_error``,
``_resolve_project``, ``_refuse_unless_project_scope`` and ``_loaded_modules``
are defined by ``core.main``, which imports THIS module — a module-level import
back would be a cycle. Importing them at call time also keeps them at the
address the suites already patch (``monkeypatch.setattr(core.main,
"_resolve_project", ...)``, twenty-one sites): a tool that captured the helper at
import time would silently ignore the patch and read production. This is the
same late-import idiom the rest of ``server/core`` uses for ``core.db``.
"""

from __future__ import annotations

import json

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent


def _flow_identity() -> str:
    """Resolve caller identity from the access token (AD-14)."""
    token: AccessToken | None = get_access_token()
    return (token.claims.get("sub") or token.client_id) if token else "anonymous"


def flows_list(project_id: str, kind: str | None = None) -> ToolResult:
    """List data-flow summaries for a project (Story 8.7, AC4).

    Returns a lean <=30-line text summary plus the full summary list in the
    canonical AD-1 envelope (structuredContent). ``kind`` filters to
    'datastream' or 'report' when given.

    AD-5: scope-checked via identity_can_read_project; a cross-project access
    surfaces as a not-found error (existence not disclosed).
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import flows as _flows  # noqa: PLC0415
    from core.main import _envelope, _error, _resolve_project  # noqa: PLC0415

    identity = _flow_identity()
    project_id = _resolve_project(project_id, identity)
    if kind is not None and kind not in ("datastream", "report"):
        raise ToolError(json.dumps(_error(
            "invalid_input", "kind must be 'datastream' or 'report'")))

    try:
        with _core_db.request_connection(identity) as conn:
            items = _flows.list_flows(project_id, identity, conn, kind=kind)
    except _flows.FlowScopeError:
        raise ToolError(json.dumps(_error("not_found", "Project not found")))
    except Exception as exc:
        raise ToolError(json.dumps(_error("db_error", f"Database error: {exc}")))

    lines = [f"Flows of project {project_id}: {len(items)}"]
    for it in items[:25]:
        lines.append(
            f"- [{it.get('kind')}] {it.get('id')} : "
            f"{it.get('name') or it.get('display_name') or it.get('base_report_id') or ''}"
        )
    summary = "\n".join(lines[:30])

    envelope = _envelope(
        {"project_id": project_id, "kind": kind, "flows": items, "count": len(items),
         "identity": identity},
        provenance={"source_system": "connector-core", "source_field": "flows_list",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


def flows_get(project_id: str, kind: str, id: str) -> ToolResult:
    """Get one full declarative flow document (Story 8.7, AC4).

    datastream -> the datastream row + mappings as a flow doc.
    report     -> the connector's base pack MERGED with the stored project
                  override.

    Lean text summary + full document in structuredContent (AD-1). AD-5 scoped.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import flows as _flows  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _error,
        _loaded_modules,
        _refuse_unless_project_scope,
        _resolve_project,
    )

    identity = _flow_identity()
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity)
    if kind not in ("datastream", "report"):
        raise ToolError(json.dumps(_error(
            "invalid_input", "kind must be 'datastream' or 'report'")))

    try:
        with _core_db.request_connection(identity) as conn:
            flow = _flows.get_flow(
                project_id, kind, id, identity, conn,
                loaded_modules=_loaded_modules,
            )
    except _flows.FlowScopeError:
        raise ToolError(json.dumps(_error("not_found", f"Flow '{kind}/{id}' not found")))
    except Exception as exc:
        raise ToolError(json.dumps(_error("db_error", f"Database error: {exc}")))

    if flow is None:
        raise ToolError(json.dumps(_error("not_found", f"Flow '{kind}/{id}' not found")))

    _flow_label = flow.get("name") or flow.get("display_name") or id
    summary = f"Flow {kind}/{id} (project {project_id}): {_flow_label}"
    envelope = _envelope(
        {"project_id": project_id, "kind": kind, "id": id, "flow": flow,
         "identity": identity},
        provenance={"source_system": "connector-core", "source_field": "flows_get",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


def flows_validate(definition: dict) -> ToolResult:
    """Dry-run validation of a flow document (Story 8.7, AC4). No DB touch.

    Returns actionable errors (json-path + French message). This is the SAME
    validation path the REST endpoint and flows_upsert use -- one common contract.
    """
    from core import flows as _flows  # noqa: PLC0415
    from core.main import _envelope  # noqa: PLC0415

    ok, errors = _flows.validate_flow(definition)
    if ok:
        summary = "Validation OK: the flow document conforms to the schema."
    else:
        summary = "Validation failed:\n" + "\n".join(
            f"- {e['path']}: {e['message']}" for e in errors[:20]
        )
    envelope = _envelope(
        {"ok": ok, "errors": errors},
        provenance={"source_system": "connector-core", "source_field": "flows_validate",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text="\n".join(summary.split("\n")[:30]))],
        structured_content=envelope,
    )


def flows_upsert(project_id: str, definition: dict) -> ToolResult:
    """Validate + upsert a flow document (Story 8.7, AC4).

    Single write path: datastream flows go through core.datastreams +
    core.datamodel; report overrides land in app.report_overrides. Validates
    against the schema BEFORE any DB touch; audits 'flow_updated' with a minimal
    before/after diff; idempotent (same doc twice -> changed:false, no audit).

    AD-3: cannot touch authorization secrets (schema rejects unknown fields;
    connection_ref_id is an opaque, project-scoped reference). AD-5 scoped.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import flows as _flows  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _error,
        _loaded_modules,
        _refuse_unless_project_scope,
        _resolve_project,
    )

    identity = _flow_identity()
    project_id = _resolve_project(project_id, identity)
    # Story 53.1: `flows_list` carried `identity_can_read_project` and the WRITE
    # next to it carried nothing -- the read was guarded, the write was not.
    _refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    try:
        with _core_db.request_connection(identity) as conn:
            result = _flows.upsert_flow(
                project_id, definition, identity, conn,
                loaded_modules=_loaded_modules,
            )
    except _flows.FlowValidationError as exc:
        raise ToolError(json.dumps({
            "code": "validation_error", "message": str(exc), "errors": exc.errors}))
    except _flows.FlowScopeError:
        raise ToolError(json.dumps(_error("not_found", "Flow not found")))
    except _flows.FlowConflictError as exc:
        raise ToolError(json.dumps({"code": "conflict", "message": str(exc)}))
    except Exception as exc:
        raise ToolError(json.dumps(_error("db_error", f"Database error: {exc}")))

    changed = result.get("changed")
    verb = "updated" if changed else "unchanged (idempotent)"
    summary = f"Flow {result.get('kind')}/{result.get('id')} {verb}."
    envelope = _envelope(
        {"project_id": project_id, **result, "identity": identity},
        provenance={"source_system": "connector-core", "source_field": "flows_upsert",
                    "pull_id": None},
        freshness="live",
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the four flow tools on the given FastMCP app (declared -- AD-43).

    Three reads and one write, and the write is why this file changed. Until
    2026-08-12 all four reached the catalog on plain ``mcp.tool``, so an
    undeclared tool that rewrites a Datastream flow document sat in the default
    Insights catalog of every host. ``_assert_consistent`` has always refused an
    ``insights`` tool with a mutating effect; the omission was what let this one
    past. Declared for what it is, ``flows_upsert`` leaves the default catalog:
    the console keeps the same write. The AD-27 ceremony its declaration
    announces is played by the RUNTIME -- verified interactive presence,
    refused centrally at ``on_call_tool`` (mcp-tool-surface.md, amendment of
    2026-08-31: ``confirmation_mode`` defined once); a per-call two-step is
    owed only where a surface's own document demands it, which none does here.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        flows_list,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        flows_get,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        flows_validate,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        flows_upsert,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="host",
    )
