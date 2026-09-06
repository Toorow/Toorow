"""toorow -- the Project Data surface on the MCP door: `get_data_surface`.

WHY IT EXISTS. The audit of 2026-08-17 (`reviews/audit-2026-08-17/04-data.md`,
"MCP : aucun outil n'appelle `compose_data_surface`") measured the gap: the Data
screen composes its six lenses -- overview, datastreams, sources, imports,
events, connectors -- through `core.data_surface.compose_data_surface`, and no
MCP tool called that function. The domain WAS reachable from an agent, but only
through other seams that answer other questions: `get_datastream_readiness`
(operations), `datastream_diagnose`, `schedule_mcp`, `inbound_mcp`. The Imports,
Sources and Connectors lenses had no projection at all, so an agent could not
read what a person reads on the screen they are both talking about.

ONE COMPOSER, NOT A SECOND MODEL. This tool calls the SAME function the console
routes call, with the same arguments. A second derivation would be a second
truth: the screen and the agent would eventually disagree about how many imports
landed, or about which source account a Datastream reads. That divergence is the
exact defect the audit of the same day found elsewhere, and it is cheaper never
to create it than to reconcile it later.

`can_edit=False`, ALWAYS. The composer emits `allowed_actions` for a caller that
may act. This door reads; it does not create a Datastream, connect a source or
launch an import. Passing `can_edit=True` here would advertise gestures this
tool cannot perform -- a catalogue entry that names an action it does not carry
is worse than one that stays silent.

PROFILE `operations`, NOT `insights`. This reads the operational state of
collection -- what ran, what landed, what is connected -- which is the question
the whole `operations_mcp` family already answers under that profile
(`get_datastream_readiness`, `list_datastream_runs`,
`get_connector_activation_status`). Putting the same question under a different
profile because it is asked by a different tool would make the profile a property
of the module rather than of the question. It also keeps the default catalogue
budget (`mcp-tool-surface.md`) unchanged for a host that never asked.

Conventions mirror `project_posture_mcp`: `from __future__ import annotations`,
module logger, `core.*` imports LAZY inside function bodies (no import cycle with
`core.main`), ASCII-only source, and registration through `register_profiled`
(AD-42/AD-43). No provider name appears in the tool name: the lens vocabulary is
the product's own.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: The lens vocabulary this door accepts, in the order the screen presents them.
#: `overview` is the census across the five; the other five are collections.
#: Kept as a tuple HERE only to name them in a refusal -- the composer remains
#: the authority on which lens exists, and a lens added there is refused here
#: with a sentence naming the accepted set rather than resolving to the wrong one.
_LENSES = ("overview", "datastreams", "sources", "imports", "events", "connectors")

#: How many items ride the text channel. The full page rides
#: `structuredContent`; the summary is an index, never the corpus (AD-1, and the
#: same rule `context_hub_mcp._discovery_summary` holds for the Hub).
_SUMMARY_MAX_LINES = 12


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": "live", "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _summary(lens: str, envelope: dict) -> str:
    """An index of what came back, bounded, with the truncation SAID."""
    items = envelope.get("items") or []
    unavailable = envelope.get("unavailable_reasons") or []
    meta = envelope.get("collection_meta") or {}
    total = meta.get("total", len(items))

    if not items:
        # An empty list says WHY, in the composer's own words, and the composer
        # names the gesture that fills it. Inventing a second sentence here would
        # be a second empty-state vocabulary for one screen.
        reason = unavailable[0].get("message") if unavailable else "Nothing to show."
        return f"Data / {lens}: empty. {reason}"

    lines = [f"Data / {lens}: {len(items)} shown of {total}."]
    for item in items[: _SUMMARY_MAX_LINES - 1]:
        ref = item.get("object_ref") or {}
        states = item.get("states") or {}
        state_text = ", ".join(f"{axis}={value}" for axis, value in sorted(states.items()))
        label = item.get("name") or item.get("label") or ref.get("id") or "(unnamed)"
        lines.append(f"- {label} [{state_text}]" if state_text else f"- {label}")
    hidden = len(items) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    return "\n".join(lines)


def get_data_surface(
    project_id: str,
    lens: str = "overview",
    object_id: str | None = None,
    limit: int | None = None,
    cursor: int = 0,
):
    """Read what the Project's Data screen shows -- imports, sources, connectors.

    Same composition as the screen, from the same function, so an agent and a
    person never disagree about the state of collection.

    `lens` selects one of six readings:
      * `overview`  -- the census: how many objects each lens holds, the state
        counts per axis, and the freshness of the evidence behind them;
      * `datastreams` -- what this Project collects, with its plan, its
        schedule and its published execution;
      * `sources` -- the connected accounts, and how many Datastreams read
        each one;
      * `imports` -- what landed, when, and whether it landed whole;
      * `events` -- the Event Configurations the Datastreams own;
      * `connectors` -- what is installed and activated for this Project.

    `object_id` reads ONE object of the chosen lens instead of the collection
    (not accepted on `overview`, which has no object detail). `limit` and
    `cursor` page a collection; the page always carries the TRUE total, so a
    bounded list never reads as a smaller Project than the one that exists.

    An empty lens says why it is empty and names the gesture that fills it.
    Missing evidence reads as unavailable -- never as healthy, never as zero.

    Pure read: nothing is created, connected, scheduled or launched. This door
    does not act on what it shows; the gestures live on the console and on the
    operations tools that own them. A Project the caller may not view answers
    `project_not_found` -- the same answer as a Project that does not exist.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    chosen = (lens or "overview").strip() or "overview"
    if chosen not in _LENSES:
        raise _tool_error(
            "unknown_lens",
            "lens must be one of: " + ", ".join(_LENSES) + ".",
        )
    if chosen == "overview" and object_id:
        raise _tool_error(
            "no_object_detail",
            "The Data overview has no object detail. Choose a lens first, then "
            "name an object_id within it.",
        )

    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core.data_surface import (  # noqa: PLC0415
        DataObjectNotFound,
        compose_data_surface,
    )

    try:
        with request_connection(identity) as conn:
            envelope = compose_data_surface(
                checked,
                chosen,
                conn,
                object_id=(object_id or None),
                can_edit=False,
                limit=limit,
                cursor=int(cursor or 0),
            )
    except DataObjectNotFound as exc:
        # The object is named by the caller, inside a Project the caller was
        # already allowed to view -- so "this object does not exist here" is
        # not an enumeration oracle, and saying it plainly is what lets the
        # caller correct the id instead of guessing.
        raise _tool_error(
            "object_not_found",
            f"No {chosen} object with this id in this Project.",
        ) from exc
    except ValueError as exc:
        # The composer's own refusals (an out-of-range cursor, an unsupported
        # lens it alone knows about). Its sentence is reused rather than
        # rewritten: two doors that phrase one refusal differently teach the
        # caller two vocabularies for one product.
        raise _tool_error("invalid_param", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error(
            "data_surface_mcp: compose failed lens=%s: %s", chosen, type(exc).__name__
        )
        raise _tool_error(
            "seam_unavailable", "The Data surface is unavailable."
        ) from exc

    return _result(_summary(chosen, envelope), envelope)


def register(mcp) -> None:
    """Register `get_data_surface` on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`, so the boot-time
    validator sees the declaration. `effect="read"` / `confirmation_mode="none"`:
    the composer only reads, and a read transitions no domain state, so there is
    nothing for a human to authorize.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_data_surface,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
