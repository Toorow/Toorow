"""toorow -- the language dimension family on the MCP surface: `read_language_bindings`.

WHY IT EXISTS. `core.language_dimensions` declares three dimensions that share
one English word and must never be merged, summed or compared with each other:
the language OBSERVED on the person reached, the language of the ASSET served,
and the language DECLARED as targeted. An agent reading a project's numbers had
no way to learn which of the three a column carries -- the whole binding
lifecycle had ZERO production callers when the audit measured it on 2026-08-17,
so nothing outside `query_specs`' comparability guard knew the family existed.
An agent that cannot tell an intent from an observation will happily narrate the
gap between them as an error to fix, which is exactly the reading the module was
written to prevent.

ONE STORE, NOT A SECOND MODEL. This tool calls `language_dimensions.list_bindings`
and `resolve_field_bindings` -- the same functions the REST surface
(`language_bindings_api`) and the conformance path call. A second derivation would
let an agent and the console disagree about what a column means.

WHAT IT DOES NOT DO. It does not declare, confirm or retire a binding.
`effect="read"`, and Insights is therefore the only legal profile. The declaring
gesture is a human act and lives on the project REST surface, whose write gate is
org-manage; an agent that could bind a column to a dimension would be deciding
what a client's numbers mean.

BOUNDED, BECAUSE A CATALOGUE HAS A COST. A project can bind many columns; the
tool returns the first `_BINDING_LIMIT` and always ships the TRUE `total` and
`has_more`, so a truncated list is never mistaken for a smaller project.

Conventions mirror `project_posture_mcp`: `from __future__ import annotations`,
module logger, `core.*` imports LAZY inside function bodies (no import cycle with
`core.main`), ASCII-only source, English microcopy, and the single registration
through `register_profiled` (AD-42/AD-43) -- a bare `mcp.tool` escapes the
capability middleware, and since AD-43 an undeclared tool reaches nobody.

No production identifier appears here: the project is a caller-supplied opaque
identifier and every dimension name is platform vocabulary.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

# How many binding rows ride the envelope. The TRUE total always rides with them.
_BINDING_LIMIT = 25


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token."""
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _family() -> list[dict]:
    """The three dimensions and what each is ABOUT -- why they never combine."""
    from core.language_dimensions import (  # noqa: PLC0415
        LANGUAGE_DIMENSION_FAMILY,
    )

    return [
        {
            "dimension": identifier,
            "nature": declared.nature,
            "definition": declared.definition,
        }
        for identifier, declared in LANGUAGE_DIMENSION_FAMILY.items()
    ]


def _org_of(project_id: str, identity: str) -> str | None:
    """The org anchor of *project_id*, read after the guard has already passed.

    A PROJECT-scoped binding is stored WITH its org, and that is not decoration:
    the RLS policy of migration 274 treats `org_id IS NULL` as a row belonging to
    no tenant and therefore visible to every one of them. So the writer sets it,
    and every reader must key on the same triplet -- `list_bindings` matches
    `COALESCE(org_id, '')`, and a read that passed NULL here would silently miss
    every row the REST surface wrote.
    """
    from core.db import request_connection  # noqa: PLC0415

    with request_connection(identity) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _row(record: dict) -> dict:
    """Project one stored binding to the bounded shape the model reads."""
    return {
        "id": record.get("id"),
        "connector": record.get("connector"),
        "report_id": record.get("report_id"),
        "source_field": record.get("source_field"),
        "canonical_dimension": record.get("canonical_dimension"),
        "status": record.get("status"),
        "reviewed_by": record.get("reviewed_by"),
        "reviewed_at": record.get("reviewed_at"),
    }


def _summary(rows: list[dict], total: int, applied: int) -> str:
    """One lean line an agent can act on without parsing the envelope."""
    if total == 0:
        return (
            "No language binding is declared on this project: no column is known "
            "to carry an audience, content or targeting language."
        )
    kinds = sorted(
        {
            str(row.get("canonical_dimension"))
            for row in rows
            if row.get("canonical_dimension")
        }
    )
    return (
        f"{total} language binding(s) declared, {applied} confirmed and applied at read; "
        f"dimensions in use: {', '.join(kinds) if kinds else 'none confirmed'}. "
        "The three are never summed or compared with each other."
    )


def read_language_bindings(project_id: str, status: str | None = None):
    """Read which source columns this project bound to which LANGUAGE dimension.

    Language is several concepts under one word, and the three are never
    merged, summed or compared with each other:

      * `audience_language` -- OBSERVED on the person reached (a browser or
        device setting). A measurement.
      * `content_language` -- a property of the ASSET actually served.
      * `targeting_language` -- DECLARED intent, true even when nobody
        matching it was reached.

    So a difference between what was targeted and what was reached is an
    INFORMATION, never an error to reconcile: do not report it as a gap to
    close, and never total two of these dimensions together.

    Returns the `family` (the three, with what each is about), the project's
    `bindings` (first 25, with the true `total` and `has_more`), and
    `applied`, the count actually resolving at read -- only a CONFIRMED
    binding resolves, so a proposed or rejected row changes no reading.

    Optional `status` filters the list (`proposed`, `pending`, `confirmed`,
    `rejected`, `excluded`).

    Pure read. Declaring or retiring a binding is a human act on the project
    surface, not something an agent performs. A project the caller may not
    view is `not_found` -- the same answer as a project that does not exist.
    """
    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")

    from core.mcp_scope import refuse_unless_project_scope  # noqa: PLC0415

    identity = _identity()
    refuse_unless_project_scope(checked, identity)

    from core.language_dimensions import (  # noqa: PLC0415
        SCOPE_PROJECT,
        list_bindings,
        resolve_field_bindings,
    )

    wanted = (status or "").strip() or None
    try:
        stored = list_bindings(
            scope_level=SCOPE_PROJECT,
            org_id=_org_of(checked, identity),
            project_id=checked,
            status=wanted,
        )
        applied = resolve_field_bindings(project_id=checked)
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error(
            "language_bindings_mcp: read failed: %s", type(exc).__name__
        )
        raise _tool_error(
            "seam_unavailable", "Language bindings are unavailable."
        ) from exc

    rows = [_row(record) for record in stored[:_BINDING_LIMIT]]
    data = {
        "project_id": checked,
        "family": _family(),
        "bindings": rows,
        "total": len(stored),
        "has_more": len(stored) > _BINDING_LIMIT,
        "applied": len(applied),
    }
    return _result(_summary(rows, len(stored), len(applied)), data)


def register(mcp) -> None:
    """Register `read_language_bindings` on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`, so the boot-time
    validator sees the declaration. `profile="insights"` / `effect="read"` /
    `confirmation_mode="none"`: Insights is the always-discoverable, non-risky
    profile, and this tool reads and returns. It writes nothing.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        read_language_bindings,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
