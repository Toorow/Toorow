"""`compose_dossier` -- the MCP door where a model KEEPS what it drew and wrote.

WHAT WAS MEASURED, 2026-09-02, before this file existed. A model over MCP could
read the context (`get_skills`, `get_procedure`), execute a governed query
(`execute_analyze_query_spec`), and ask for a figure (`render_analyze_result`)
-- and nothing it did survived the session. `render_analyze_result` mints a
handle and eligibility rows and writes NO `app.renders` row; `create_render`
was reachable only over `POST .../renders`; a Notebook Run records
`NO_RENDER_NOT_DISPATCHED`; and the Dossier of 2026-09-01 -- the ratified object
for "several figures and a narrative" -- had no MCP tool at all
(`grep -i dossier server/core/*mcp*.py` -> 0). This is why a view built from
widgets had never been seen: the fourth of Jean's five steps had no door.

The target is the amendment of 2026-09-02 in
`docs/product-architecture/visualization-and-rendering.md` ("the model composes
the Dossier"). Its decisions, as this module carries them:

* **D1 -- one door, and it is the fourth step.** The tool takes a `label` and an
  ordered list of blocks: a `figure` names a `result_id` and exactly one of
  `visualization_spec_version_id` / `visualization_template_version_id`; a
  `narrative` carries `text`. The server freezes ONE Render per figure through
  the same `create_render` the console uses, with pins it resolves itself from
  the delivered runtime -- the caller names no pin. Then it writes the Dossier
  version pinned to those Renders, in ONE transaction: a figure that cannot be
  frozen leaves no Render and no Dossier behind.
* **D2 -- a read does not write.** `render_analyze_result` stays a read; this
  tool is declared exactly as `save_notebook`: `operations`, `confirmed_write`,
  `confirmation_mode="host"`, refused centrally without verified presence.
* **D3 -- a narrative says who wrote it.** Every narrative block this tool
  stores carries `authored_by: "model"`, whatever the caller sent. A figure is
  governed (a pinned Result); a narrative is generated; the page shows which.
* **The bearer never travels the model channel** (2026-08-17): the answer names
  the dossier, its version, its Renders and the CONSOLE address -- never a
  share link.

Imports of the persistence layer stay inside the bodies, as `notebook_mcp` does:
`core.main` imports this module and the suites patch `core.db` at that address.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core.audit import declare_action
from core.mcp_scope import refuse_unless_project_scope

logger = logging.getLogger(__name__)

ACTION_DOSSIER_COMPOSED = declare_action("dossier_composed")

#: The word a narrative block written through this door carries -- D3. The
#: console writes `human`; a stored block without the field is read as `human`
#: because only the console wrote blocks before 2026-09-02.
NARRATIVE_AUTHOR = "model"

#: The responsive profile a figure is frozen under. The Render is replayed on
#: the console dossier page and on the share page; `console` is the profile the
#: console's own freeze pins, so a Render composed here and one frozen from the
#: Builder are the same kind of object.
FIGURE_PROFILE = "console"

#: `ck_renders_creation_surface` admits exactly `explore | report | notebook |
#: mcp`; a Render frozen by a model over MCP is the fourth. `origin_kind` has no
#: dossier value (`explore | report_run | notebook_run`) and `explore` is what a
#: Render with no Run behind it is.
CREATION_SURFACE = "mcp"
ORIGIN_KIND = "explore"

_BLOCK_KINDS = ("figure", "narrative")


def _identity() -> str:
    token: AccessToken | None = get_access_token()
    return (token.claims.get("sub") or token.client_id) if token else "anonymous"


def _org_of(conn, project_id: str) -> str | None:
    """The organization of a Project. One read, one query, nothing invented."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _tool_error(code: str, message: str, refusals: list[dict[str, str]] | None = None):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    body: dict[str, Any] = {"code": code, "message": message}
    if refusals:
        body["refusals"] = refusals
    return ToolError(json.dumps(body))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _validated_shape(blocks: Any) -> list[dict[str, Any]]:
    """The blocks as this tool reads them, or EVERY shape refusal at once.

    Shape only -- no database. Existence of a Result, compatibility of a Spec,
    the runtime's pins: each of those is refused later by the function that owns
    it, on the armed connection. A model that sent twelve malformed blocks learns
    its twelve errors in one turn, the way `save_notebook` answers.
    """
    if not isinstance(blocks, list) or not blocks:
        raise _tool_error(
            "missing_blocks",
            "blocks is an ordered list: at least one figure, and any narrative.",
        )
    refusals: list[dict[str, str]] = []
    normalized: list[dict[str, Any]] = []
    figures = 0
    for index, block in enumerate(blocks):
        subject = f"blocks[{index}]"
        if not isinstance(block, dict) or block.get("kind") not in _BLOCK_KINDS:
            refusals.append(
                {
                    "code": "unknown_block_kind",
                    "message": "a block is `figure` or `narrative`",
                    "subject": subject,
                }
            )
            continue
        if block["kind"] == "narrative":
            text = _text(block.get("text"))
            if not text:
                refusals.append(
                    {
                        "code": "empty_narrative",
                        "message": "a narrative block carries text",
                        "subject": subject,
                    }
                )
                continue
            # D3: whatever the caller wrote in `authored_by`, this door wrote it.
            normalized.append({"kind": "narrative", "text": text, "authored_by": NARRATIVE_AUTHOR})
            continue
        result_id = _text(block.get("result_id"))
        spec_version_id = _text(block.get("visualization_spec_version_id"))
        template_version_id = _text(block.get("visualization_template_version_id"))
        if not result_id:
            refusals.append(
                {
                    "code": "missing_result_id",
                    "message": "a figure names the Result it shows",
                    "subject": subject,
                }
            )
            continue
        if bool(spec_version_id) == bool(template_version_id):
            refusals.append(
                {
                    "code": "visualization_pin_ambiguous",
                    "message": (
                        "name the presentation once: visualization_spec_version_id for a "
                        "Visualization that exists, or visualization_template_version_id "
                        "to apply a Chart Template -- exactly one"
                    ),
                    "subject": subject,
                }
            )
            continue
        figures += 1
        normalized.append(
            {
                "kind": "figure",
                "result_id": result_id,
                "visualization_spec_version_id": spec_version_id or None,
                "visualization_template_version_id": template_version_id or None,
            }
        )
    if refusals:
        raise _tool_error("invalid_blocks", f"{len(refusals)} block(s) refused by shape", refusals)
    if figures == 0:
        raise _tool_error(
            "no_figure_block",
            "a Dossier composes at least one figure; a narrative alone is a note.",
        )
    return normalized


def _freeze_figure(
    conn, *, identity: str, org_id: str, project_id: str, figure: dict[str, Any], subject: str
) -> str:
    """Freeze ONE Render for one figure, with pins the server resolves. Returns its id.

    The same reads as `render_analyze_result` (the Result envelope, its retained
    payload, the Spec version -- materialised from a Chart Template when that is
    what the figure named), the same pin resolver over the delivered runtime
    manifest, and then `create_render`, which refuses pin by pin. Nothing here is
    a second authority on what was drawn: the runtime build, renderer build,
    theme and formatter are read off the bundle this deployment serves.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import render_app_payload, visualization_specs  # noqa: PLC0415
    from core.analyze_artifacts import (  # noqa: PLC0415
        ArtifactNotFound,
        ArtifactRefused,
        create_render,
    )
    from core.analyze_render_mcp import (  # noqa: PLC0415
        _load_payload,
        _load_result,
        _materialize_pinned_spec,
        _semantic_view_version,
    )

    try:
        result = _load_result(
            conn, org_id=org_id, project_id=project_id, result_id=figure["result_id"]
        )
        payload = _load_payload(
            conn, org_id=org_id, project_id=project_id, result_id=figure["result_id"]
        )
    except ToolError:
        raise _tool_error(
            "result_not_found",
            "this figure names a Result this Project does not hold",
            [{"code": "result_not_found", "message": figure["result_id"], "subject": subject}],
        ) from None
    if result["outcome"] != "success":
        raise _tool_error(
            "figure_result_not_success",
            f"a figure freezes a successful Result; this one is `{result['outcome']}`",
            [
                {
                    "code": "figure_result_not_success",
                    "message": figure["result_id"],
                    "subject": subject,
                }
            ],
        )
    if result["content_hash"] != payload["content_hash"]:
        raise _tool_error(
            "render_result_identity_mismatch",
            "The frozen Result identity is inconsistent. Re-run the analysis.",
            [
                {
                    "code": "render_result_identity_mismatch",
                    "message": figure["result_id"],
                    "subject": subject,
                }
            ],
        )

    spec_version_id = figure["visualization_spec_version_id"] or _materialize_pinned_spec(
        conn,
        identity=identity,
        org_id=org_id,
        project_id=project_id,
        result_id=result["id"],
        template_version_id=figure["visualization_template_version_id"],
    )
    try:
        spec_version = visualization_specs.load_visualization_spec_version(
            conn,
            org_id=org_id,
            project_id=project_id,
            visualization_spec_version_id=spec_version_id,
        )
    except visualization_specs.VisualizationNotFound:
        raise _tool_error(
            "visualization_spec_not_available",
            "The Visualization Spec is not available in this Project. Choose another visual.",
            [
                {
                    "code": "visualization_spec_not_available",
                    "message": spec_version_id,
                    "subject": subject,
                }
            ],
        ) from None
    if spec_version["query_spec_version_id"] != result["query_spec_version_id"]:
        raise _tool_error(
            "incompatible_visualization_spec",
            "The Visualization Spec was built for another Query Spec. Choose a compatible visual.",
            [
                {
                    "code": "incompatible_visualization_spec",
                    "message": spec_version_id,
                    "subject": subject,
                }
            ],
        )

    try:
        manifest = render_app_payload.load_runtime_manifest()
        pins = render_app_payload.resolve_runtime_pins(
            manifest,
            family=spec_version["family"],
            schema_version=spec_version["schema_version"],
            profile=FIGURE_PROFILE,
        )
    except render_app_payload.RenderAppPayloadRefused as exc:
        raise _tool_error(
            exc.code, str(exc), [{"code": exc.code, "message": str(exc), "subject": subject}]
        ) from exc

    semantic_view_version_id = _semantic_view_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        query_spec_version_id=result["query_spec_version_id"],
    )
    render_payload = {
        "result_id": result["id"],
        "result_content_hash": payload["content_hash"],
        "visualization_spec_version_id": spec_version["id"],
        "renderer_build_id": pins["renderer_build"],
        "runtime_build_id": pins["runtime_build"],
        "theme_version": pins["theme_version"],
        "formatter_version": pins["formatter_version"],
        "responsive_profile": FIGURE_PROFILE,
        "display_state": {},
        # What the console's freeze pins, plus the two identities a dossier page
        # prints beside the figure: the Semantic View version and the walk.
        "evidence_manifest": {
            "result_id": result["id"],
            "result_content_hash": payload["content_hash"],
            "query_spec_version_id": result["query_spec_version_id"],
            "visualization_spec_version_id": spec_version["id"],
            "semantic_view_version_id": semantic_view_version_id or "unavailable",
            "ai_path": result["ai_path"],
            "composed_by": "compose_dossier",
        },
        "datum_evidence_keys": {},
        "creation_surface": CREATION_SURFACE,
        "origin_kind": ORIGIN_KIND,
    }
    try:
        created = create_render(
            conn, org_id=org_id, project_id=project_id, actor=identity, payload=render_payload
        )
    except ArtifactNotFound:
        raise _tool_error(
            "result_not_found",
            "this figure names a Result this Project does not hold",
            [{"code": "result_not_found", "message": figure["result_id"], "subject": subject}],
        ) from None
    except ArtifactRefused as refused:
        body = refused.as_dict()
        raise _tool_error(
            body.get("code", "incomplete_render"),
            f"{subject}: {body.get('message', 'the Render was refused')}",
            body.get("refusals"),
        ) from None
    return str(created["id"])


def _bounded_evidence(
    *,
    version_id: str,
    version_number: int,
    render_ids: list[str],
    narrative_count: int,
) -> list[dict[str, Any]]:
    """Bounded evidence BESIDE the deep link, never replaced by it (AC9).

    `model_channel.enforce_model_channel` refuses a payload that offers a
    `deep_link` with an empty `evidence`, so a host with no UI can still check
    what was composed. Same `{column, first_value}` vocabulary as
    `analyze_render_mcp.bounded_evidence`, so a reader meets one shape.

    Measured 2026-09-04: G15-T03 was refused `evidence_required_with_deep_link`
    on the deployment while the unit test, which called the tool without the
    profiled wrapper, stayed green. It is a named function so a test can empty
    it and prove the guard the profiled surface applies is actually attached --
    the refusal now runs in `tests/core/test_dossier_mcp_pg.py` rather than only
    on the deployment.
    """
    return [
        {"column": "dossier_version_id", "first_value": version_id},
        {"column": "version_number", "first_value": version_number},
        {"column": "render_ids", "first_value": ", ".join(render_ids)},
        {"column": "narrative_blocks", "first_value": narrative_count},
    ]


def compose_dossier(
    project_id: str,
    label: str,
    blocks: list,
    description: str = "",
    dossier_id: str = "",
) -> ToolResult:
    """Keep figures and your commentary as one Dossier. Writes to PostgreSQL.

    Freezes one Render per figure (the server resolves every replay pin from the
    deployed runtime) and writes a Dossier version pinned to those Renders, in
    one transaction. Narrative blocks are stored verbatim and marked as written
    by the model. Answers the dossier, version and Render identities and the
    console address of the dossier -- never a share link.

    Parameters:
        project_id:  Project identifier.
        label:       The Dossier's name, as a person reads it.
        blocks:      1..64 ordered blocks. `{"kind": "figure", "result_id": ...,
                     "visualization_spec_version_id": ...}` (or
                     `visualization_template_version_id` instead, never both)
                     freezes a figure; `{"kind": "narrative", "text": ...}`
                     keeps your commentary at that place in the document.
                     At least one figure.
        description: Optional. What this Dossier answers.
        dossier_id:  Optional. An existing Dossier to succeed with a new
                     version; absent, a new Dossier is created.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core.analyze_artifacts import ArtifactNotFound, ArtifactRefused  # noqa: PLC0415
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.dossiers import append_dossier_version, create_dossier  # noqa: PLC0415
    from core.main import _resolve_project  # noqa: PLC0415

    identity = _identity()
    project_id = _resolve_project(project_id, identity)
    # A WRITE, so `edit`, through the seam that fails CLOSED (AI-269).
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    label = _text(label)
    if not label:
        raise _tool_error("missing_field", "a Dossier needs a label.")
    shaped = _validated_shape(blocks)
    dossier_id = _text(dossier_id) or None

    render_ids: list[str] = []
    try:
        with request_connection(identity) as conn:
            org_id = _org_of(conn, project_id)
            if org_id is None:
                raise _tool_error("not_found", "Project not found or archived.")
            stored_blocks: list[dict[str, Any]] = []
            for index, block in enumerate(shaped):
                if block["kind"] == "narrative":
                    stored_blocks.append(block)
                    continue
                render_id = _freeze_figure(
                    conn,
                    identity=identity,
                    org_id=org_id,
                    project_id=project_id,
                    figure=block,
                    subject=f"blocks[{index}]",
                )
                render_ids.append(render_id)
                stored_blocks.append({"kind": "render", "render_id": render_id})
            if dossier_id:
                version = append_dossier_version(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    dossier_id=dossier_id,
                    actor=identity,
                    blocks=stored_blocks,
                    label=label,
                    description=description or None,
                    narrative_author=NARRATIVE_AUTHOR,
                )
            else:
                version = create_dossier(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    label=label,
                    actor=identity,
                    blocks=stored_blocks,
                    description=description or None,
                    narrative_author=NARRATIVE_AUTHOR,
                )
            conn.commit()
    except ArtifactNotFound:
        raise _tool_error("not_found", "Dossier not found in this Project.") from None
    except ArtifactRefused as refused:
        raise ToolError(json.dumps(refused.as_dict())) from None
    except ToolError:
        raise
    except Exception as exc:
        logger.warning("compose_dossier: db_write_failed: %s", exc)
        raise _tool_error("db_error", f"Database write failed: {exc}") from None

    dossier_id = str(version.get("dossier_id") or dossier_id or "")
    version_id = str(version.get("id") or "")
    version_number = version.get("version_number")
    narrative_count = sum(1 for b in shaped if b["kind"] == "narrative")
    write_audit_row(
        identity=identity,
        action=ACTION_DOSSIER_COMPOSED,
        provider_account="",
        connection_ref="",
        metadata={
            "dossier_id": dossier_id,
            "dossier_version_id": version_id,
            "version_number": version_number,
            "project_id": project_id,
            "label": label,
            "render_ids": render_ids,
            "narrative_blocks": narrative_count,
        },
    )

    from core.project_overview import owner_reference  # noqa: PLC0415

    # The same shape as `analyze_render_mcp.result_deep_link`: a structured owner
    # reference the console resolves against its navigation registry -- never a
    # URL minted here. A Dossier is an object of `analyze/renders` (73-2, 74-2).
    deep_link = {
        "kind": "console",
        "requires_authenticated_session": True,
        "organization_id": org_id,
        "project_id": project_id,
        "object_type": "dossier",
        "object_id": dossier_id,
        "tab": "document",
        "owner_reference": owner_reference(
            "analyze", "renders", object_type="dossier", object_id=dossier_id, tab="document"
        ),
    }
    text = (
        f"Dossier '{label}' composed: version {version_number} ({version_id}) of {dossier_id}, "
        f"{len(render_ids)} figure{'' if len(render_ids) == 1 else 's'} frozen as "
        f"{', '.join(render_ids)} and {narrative_count} narrative block"
        f"{'' if narrative_count == 1 else 's'} marked as written by the model. "
        f"A person opens it in the console under Analyze > Dossiers; sharing outside "
        f"the organization is a console act."
    )
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={
            "dossier_id": dossier_id,
            "dossier_version_id": version_id,
            "version_number": version_number,
            "render_ids": render_ids,
            "narrative_blocks": narrative_count,
            "evidence": _bounded_evidence(
                version_id=version_id,
                version_number=version_number,
                render_ids=render_ids,
                narrative_count=narrative_count,
            ),
            "deep_link": deep_link,
        },
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind `compose_dossier`, declared exactly as `save_notebook` is (D2)."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        compose_dossier,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="host",
    )
