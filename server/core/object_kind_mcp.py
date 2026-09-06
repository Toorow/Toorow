"""A client object kind over MCP -- the model's door onto Story 64.1.

WHY A SECOND MODULE AND NOT THREE MORE TOOLS IN `master_data_mcp`. That module is
already correct and already thin: it re-implements no validation and hands over to
``country_workspace_commands``, the ONE command runner. What is country-shaped
there is not the plumbing, it is the runner -- ``apply_preset``,
``save_hierarchy``, ``publish`` are Country's verbs. A client object kind has
different verbs (declare it, release its source, describe it), served by
:mod:`core.object_kind_registry`, so it gets its own door onto ITS runner rather
than a fourth action bolted onto Country's.

WHAT IS COPIED EXACTLY, AND WHY IT MUST BE. The authorization is the neighbour's,
verbatim: ``resolve_strict_resource_access`` with ``hold_access=True``, the same
capability mapping, and deny-by-default answering ``not_found`` rather than
``forbidden`` -- the existence of another Project's master data is itself
sensitive. A second door with a softer guard is not a second door, it is a way
around the first one.

NOTHING HERE KNOWS WHAT A VIDEO IS. ``object_kind`` travels as an opaque string,
exactly as the generic core treats it (``master_data.py``: the kinds "are opaque
strings validated for shape and never compared to a literal here"). A test asserts
that no kind literal appears in this module, because the whole point of Story 64.1
is that the client names the object, not the code.

THE CAPABILITY SPLIT, AND THE ONE JUDGEMENT IT CONTAINS. Declaring a kind is an
``edit``: it mints an owner and binds a source, both reversible and both dated.
RELEASING a source binding is a ``manage``: it retires the answer to "what
identifies this object", every alias resolved against that identity (Story 64.10)
depends on it, and the act is the one an agent must not reach on its own.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: `release` sits with publish-class acts: it changes what an identity MEANS.
#: Declaring only adds one, and a wrong declaration is released, not suffered.
_MANAGE_ACTIONS = frozenset({"release_source"})


def _tool_error(code: str, message: str):
    import json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def minimum_capability(action: str) -> str:
    """Module-level so it is testable for what it is, like `refuse_manage_action`.

    Buried in a closure, the split between `edit` and `manage` would be provable
    only by reaching the database.
    """
    if action in _MANAGE_ACTIONS:
        return "manage"
    return "read" if action == "describe" else "edit"


def _envelope(text: str, structured: dict[str, Any]) -> dict[str, Any]:
    """Split the short model channel from the full canonical envelope (AD-1)."""
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


def _run(action: str, project_id: str, run, *, idempotency_key: str | None = None):
    """Authorize, run inside one transaction, commit. No second scorer.

    `run(conn, org_id, actor)` is the caller's own body, so this function owns the
    guard and the transaction and knows nothing else -- the shape that keeps a
    second door from growing a second validation.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.master_data import (  # noqa: PLC0415
        MasterDataConflict,
        MasterDataError,
        MasterDataNotFound,
    )
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    actor = _identity()
    if not actor or actor == "anonymous":
        raise _tool_error("not_found", "Resource not found.")
    if action != "describe" and not str(idempotency_key or "").strip():
        raise _tool_error("missing_idempotency_key", "An idempotency key is required.")

    with request_connection(actor) as conn:
        decision = resolve_strict_resource_access(
            actor,
            conn,
            project_id=project_id,
            minimum_capability=minimum_capability(action),
            hold_access=True,
        )
        if not decision.allowed or not decision.org_id:
            # Indistinguishable from absence, exactly like the REST and Country doors.
            raise _tool_error("not_found", "Resource not found.")
        try:
            result = run(conn, str(decision.org_id), actor)
        except MasterDataNotFound as exc:
            raise _tool_error("not_found", str(exc)) from exc
        except MasterDataConflict as exc:
            raise _tool_error("conflict", str(exc)) from exc
        except MasterDataError as exc:
            # The refusals of Story 64.1 -- no grain, a repeated field, a malformed
            # kind. They travel with their sentence: an agent that is told only
            # "invalid" retries the same call.
            raise _tool_error("invalid_declaration", str(exc)) from exc
        if action != "describe":
            conn.commit()
    return result


def describe_client_object_kind(project_id: str, object_kind: str):
    """Lit un type d'objet declare : son registre, sa source, son identite, ses noeuds."""
    from core.object_kind_registry import describe_object_kind  # noqa: PLC0415

    result = _run(
        "describe",
        project_id,
        lambda conn, org_id, actor: describe_object_kind(
            conn, project_id=project_id, object_kind=object_kind
        ),
    )
    sources = result.get("sources") or []
    # One line per source: a kind fed by a keyed connector AND a named-only
    # workbook has two different answers to "how is this identified", and a
    # summary showing one of them would be the misleading half.
    shown = ", ".join(
        f"{s['namespace']}={s['identity_mode']}"
        + (f"({', '.join(s['identity_fields'])})" if s.get("identity_fields") else "")
        for s in sources
    ) or "no source"
    reason = result.get("unavailable_reason")
    text = f"{object_kind}: {shown}, {result.get('node_count', 0)} noeuds"
    if reason:
        text += f" -- {reason}"
    return _envelope(text, result)


def declare_client_object_kind(
    project_id: str,
    object_kind: str,
    label: str,
    datastream_id: str,
    mapping_version_id: str,
    namespace: str,
    identity_mode: str = "source_key",
    label_field: str | None = None,
    idempotency_key: str = "",
):
    """Declare un type d'objet du client et le lie a une source qui l'alimente.

    identity_mode : `source_key` quand la source porte une cle (la maille du
    mapping est l'identite), `governed_label` quand elle ne porte qu'un
    libelle -- `label_field` nomme alors la colonne a resoudre.
    """
    from core.object_kind_registry import declare_object_kind  # noqa: PLC0415

    result = _run(
        "declare",
        project_id,
        lambda conn, org_id, actor: declare_object_kind(
            conn,
            org_id=org_id,
            project_id=project_id,
            object_kind=object_kind,
            label=label,
            datastream_id=datastream_id,
            mapping_version_id=mapping_version_id,
            namespace=namespace,
            identity_mode=identity_mode,
            label_field=label_field,
            actor=actor,
        ),
        idempotency_key=idempotency_key,
    )
    fields = ", ".join(result.get("identity_fields") or []) or (label_field or "")
    return _envelope(
        f"{object_kind} declare depuis {namespace} -- {identity_mode} {fields}", result
    )


def release_client_object_source(
    project_id: str,
    registry_id: str,
    namespace: str,
    idempotency_key: str = "",
):
    """Retire UNE source d'un registre, nommee par son namespace.

    Le namespace est obligatoire : un registre peut avoir plusieurs sources
    vivantes, et une liberation qui ne nommerait que le registre retirerait
    celle que la base rend en premier.
    """
    from core.object_kind_registry import release_source_binding  # noqa: PLC0415

    result = _run(
        "release_source",
        project_id,
        lambda conn, org_id, actor: release_source_binding(
            conn,
            project_id=project_id,
            registry_id=registry_id,
            namespace=namespace,
            actor=actor,
        ),
        idempotency_key=idempotency_key,
    )
    return _envelope(f"source {namespace} liberee pour {registry_id}", result)


def register(mcp) -> None:
    """Register the object-kind tools under the governance profile.

    Three tools and not one, for the reason `master_data_mcp` states: a single
    tool would have to declare ONE `confirmation_mode` for acts that do not
    deserve the same. A read that asked for a human confirmation would be a lie in
    the catalogue; a release that asked for none would be a worse one.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        describe_client_object_kind,
        profile="governance",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        declare_client_object_kind,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        # `host` and not `server`, the neighbour's reasoning verbatim: the catalogue
        # validator refuses a consequential mutation without a trusted confirmation.
        # Declaring an object kind creates a governed owner in a Project -- the host
        # asks before it runs.
        confirmation_mode="host",
    )
    register_profiled(
        mcp,
        release_client_object_source,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        # `human`, like Country's publish: releasing retires the answer to "what
        # identifies this object", and every alias resolved against that identity
        # depends on it. Not an act an agent completes on its own.
        confirmation_mode="human",
    )
