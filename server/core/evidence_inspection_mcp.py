"""The `mcp_app` surface gets its writer (Story 55.2, epic 51's dimension).

Migration 175 shipped `app.evidence_inspections` with
``surface IN ('console', 'mcp_app', 'share')`` and left the middle value with no
caller, recording the reason on a constraint: an app-visibility MCP tool
supposedly "cannot be declared under the three effects of AD-24 without lying
about one of them". Migration 177 replaced that sentence, because it was not
true and had not been true since Story 50.6:

    grep -n "_record_app_declaration" -A 20 core/mcp_profiles.py
      -> an app-only tool MUST be effect="read" / confirmation_mode="none"

and `read` is honest here. AD-24's ``effect`` classifies what a tool does to
DOMAIN state; AD-28 separately REQUIRES every read to commit an append-only audit
event. An inspection is that audit row -- append-only, no state transition,
reachable by the audited RGPD erasure, "evidence that a surface was USED, never a
second copy of what it displayed" (migration 175). A vocabulary in which writing
it promoted a tool out of ``read`` would make AD-28 unimplementable for every read
tool in the catalog.

So this module is ordinary work, not a decision. Four refusals it encodes:

1. **`surface` is NOT a parameter.** It is pinned to ``mcp_app``. This tool IS
   that surface; letting the caller name its own surface would let the evidence
   say an inspection happened somewhere it did not, and the only reason the
   column exists is to tell the three apart.
2. **Nothing about WHAT was read.** The body carries the act, the honest state,
   the branch count when there was a listing, and the references. `app.ai_paths`
   and `app.ai_path_steps` already hold the walk; copying it here would be a
   second store with a second truth.
3. **The strict writer, on purpose.** `insert_inspection` raises on a refused
   vocabulary, and the console route made the same choice for the same reason: a
   client that described the inspection wrongly must be told. The display it
   belongs to has already happened, so nothing is lost by answering honestly.
4. **Project access is resolved before anything is written**, through the same
   `resolve_strict_resource_access` seam the console route uses -- not a second
   authorization path. A widget is not a trusted caller.
5. **The append is refused without a server-minted handle** (2026-08-30). Access
   says WHO may append; it does not say that an append happened at all. Until
   this refusal existed a model that knew the tool's name could record an
   inspection nobody performed, and the only thing in its way was the app-only
   discovery filter -- which `mcp-tool-surface.md` itself calls host-routing
   metadata, "not authorization". The grant is the one
   `app_read_result_manifest` / `app_read_result_slice` already require
   (`core.app_observation_handle`), and it also BINDS the row: `result_ref` is
   the Result the server issued the handle over, not a name the caller chose.
   That is what makes ``effect="read"`` below true rather than merely arguable.

Registered on `register_profiled(..., app=AppConfig(visibility=["app"]))`, so it
rides the capability middleware. `submit_feedback` does too since AD-43
(`core/feedback_mcp.py#register`) -- ce paragraphe disait l'inverse, corrige le 2026-08-22 :
il decrivait un trou refermé, et repeter une phrase perimee dans un second module
la rend deux fois plus credible.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The one surface this writer may claim. Not a parameter -- see refusal 1.
SURFACE = "mcp_app"


def _tool_error(code: str, message: str):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _unavailable():
    """One denial shape. It never says whether the Project exists."""
    return _tool_error(
        "evidence_inspection_unavailable",
        "The inspection could not be recorded.",
    )


def record_inspection_from_app(
    *,
    project_id: str,
    kind: str,
    displayed_state: str,
    handle: str = "",
    branches_listed: Any = None,
    ai_path_id: str | None = None,
    step_ordinal: Any = None,
    result_ref: str | None = None,
    render_ref: str | None = None,
) -> dict[str, Any]:
    """Write one `mcp_app` inspection and return its id. Raises a ToolError on refusal.

    Kept out of :func:`register` so it is callable from a test without a FastMCP
    server, which is what makes the surface provable offline.

    `handle` is REQUIRED (2026-08-30). Refusal 5 of the module docstring: the
    inspection may only be appended against a server-minted grant the model never
    receives. `result_ref` is bound to that grant -- empty is filled from it, and
    a value naming another Result is refused.
    """
    from core import evidence_inspections  # noqa: PLC0415
    from core.app_observation_handle import (  # noqa: PLC0415
        ObservationHandleRefused,
        consume_observation_handle,
        require_observation_handle,
        resolve_observation_handle,
    )
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_profiles import _identity  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    actor = _identity()
    project = (project_id or "").strip()
    if not project:
        raise _tool_error("missing_project", "project_id is required.")

    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project,
                minimum_capability="view",
                hold_access=True,
            )
            if not decision.allowed or not decision.org_id:
                raise _unavailable()
            # The payload complaint answers AFTER the access decision, never
            # before it: `_unavailable` is the one envelope a caller without
            # access may read, and a refusal about its own arguments arriving
            # first would say "this Project is there, your call was malformed".
            # The same order `submit_feedback` holds for `project_not_found`.
            require_observation_handle(handle)
            grant = resolve_observation_handle(
                conn,
                handle=handle,
                identity=actor,
                project_id=project,
                result_ref=result_ref,
            )
            inspection_id = evidence_inspections.insert_inspection(
                conn,
                org_id=decision.org_id,
                project_id=project,
                actor=actor,
                kind=kind,
                # Pinned, never taken from the caller.
                surface=SURFACE,
                displayed_state=displayed_state,
                branches_listed=branches_listed,
                result_ref=(result_ref or "").strip() or grant["result_id"],
                render_ref=render_ref,
                ai_path_id=ai_path_id,
                step_ordinal=step_ordinal,
            )
            consume_observation_handle(conn, handle=handle)
            conn.commit()
    except ObservationHandleRefused as exc:
        # Named before the catch-all below, which would answer the mute
        # `evidence_inspection_unavailable` and hide the repairing gesture.
        raise _tool_error(exc.code, exc.message) from exc
    except evidence_inspections.InspectionRefused as exc:
        # A refused vocabulary is the caller's error and is named as such.
        raise _tool_error(exc.code, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- fail closed without disclosing existence
        if type(exc).__name__ == "ToolError":
            raise
        logger.warning(
            "evidence_inspection_mcp: inspection not recorded project=%s: %s",
            project,
            type(exc).__name__,
        )
        raise _unavailable() from exc

    return {"inspection_id": inspection_id, "surface": SURFACE}


def app_record_evidence_inspection(
    project_id: str,
    kind: str,
    displayed_state: str,
    handle: str = "",
    branches_listed: int | None = None,
    ai_path_id: str | None = None,
    step_ordinal: int | None = None,
    result_ref: str | None = None,
    render_ref: str | None = None,
):
    """Record that somebody opened a step of a walk in the MCP App (app-only).

    Not a model-callable analytical tool: it exists for the mounted widget.
    `surface` is fixed to `mcp_app` and is not accepted from the caller. It
    records THAT a surface was used and which honest state was shown, never
    what was displayed. `displayed_state` distinguishes a listing from the
    three ways one can be empty -- in particular `branches_not_recorded`,
    which is not a zero.

    `handle` is the server-minted `result_handle` from result `_meta` and is
    required: without it the append would be something a caller could make on
    its own word, and the discovery filter is not a guard.
    """
    return record_inspection_from_app(
        project_id=project_id,
        kind=kind,
        displayed_state=displayed_state,
        handle=handle,
        branches_listed=branches_listed,
        ai_path_id=ai_path_id,
        step_ordinal=step_ordinal,
        result_ref=result_ref,
        render_ref=render_ref,
    )


def register(mcp) -> None:
    """Register the app-only `mcp_app` inspection writer.

    Called once from `core.main`, BEFORE `validate_catalog()`, like every other
    profiled registration -- a declaration the boot validator does not see is a
    declaration that proves nothing.
    """
    from fastmcp.apps import AppConfig  # noqa: PLC0415

    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        app_record_evidence_inspection,
        # `read`: it transitions no domain state. The append-only observation it
        # leaves is the AD-28 audit row, not the tool's effect -- see the module
        # docstring and `mcp_profiles._record_app_declaration`. Since 2026-08-30
        # that reading is CONDITIONAL and the condition is enforced above: no
        # server-minted handle, no append (refusal 5).
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
        app=AppConfig(visibility=["app"]),
    )
