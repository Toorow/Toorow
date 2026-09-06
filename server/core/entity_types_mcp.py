"""A declared entity type over MCP -- the model's door onto Story 68.1.

WHY ITS OWN MODULE. `object_kind_mcp` serves the declaration that BINDS a
feeding source (Story 64.1): its declare refuses a kind whose mapping carries
no identity. This story's declaration is the feeder-less half -- name,
canonical key, display label, and nothing else -- served by
``core.object_kind_registry.declare_entity_type``. Two declarations, two
doors, ONE registry: a registry IS the entity type (migration 140), and both
doors land on the same row of the same table.

WHAT IS COPIED EXACTLY, AND WHY IT MUST BE. The authorization is the
neighbour's, verbatim: ``resolve_strict_resource_access`` with
``hold_access=True``, the same capability mapping, and deny-by-default
answering ``project_not_found`` rather than ``forbidden`` -- the existence of
another Project's master data is itself sensitive, and an outage of the
decision itself fails CLOSED onto the same answer (the isolation harness of
Story 53.1 compares the two envelopes). A second door with a softer guard is
not a second door, it is a way around the first one.

THE NAMED CONFLICT SURVIVES. A replay of the SAME declaration returns the
existing type with ``replayed=True`` and writes nothing; a DIFFERENT
declaration of the same kind is refused as ``entity_type_exists``, with the
kind and its holder in the sentence. Collapsing the two into one "conflict"
code was the defect this story exists to refuse: an agent told only "exists"
cannot tell "I already did this" from "this kind means something else here".

NOTHING HERE KNOWS WHAT A VIDEO IS. ``object_kind`` travels as an opaque
string, exactly as the generic core treats it; a test asserts that no kind
literal appears in this module.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _tool_error(code: str, message: str):
    import json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def minimum_capability(action: str) -> str:
    """Module-level so it is testable for what it is, like the neighbour's.

    Declaring is an `edit`: it mints a governed owner, reversible and dated.
    Listing is a `read`. Anything else defaults to `edit` -- an unknown action
    must not fall through to the weakest guard.
    """
    return "read" if action == "list" else "edit"


def _envelope(text: str, structured: dict[str, Any]) -> dict[str, Any]:
    """Split the short model channel from the full canonical envelope (AD-1)."""
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


def _run(action: str, project_id: str, run, *, idempotency_key: str | None = None):
    """Authorize, run inside one transaction, commit. No second scorer.

    `run(conn, org_id, actor)` is the caller's own body, so this function owns
    the guard and the transaction and knows nothing else -- the shape that
    keeps a second door from growing a second validation.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.master_data import (  # noqa: PLC0415
        MasterDataConflict,
        MasterDataError,
        MasterDataNotFound,
    )
    from core.object_kind_registry import EntityTypeExists  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    actor = _identity()
    if not actor or actor == "anonymous":
        raise _tool_error("project_not_found", "Resource not found.")
    if action != "list" and not str(idempotency_key or "").strip():
        raise _tool_error("missing_idempotency_key", "An idempotency key is required.")

    with request_connection(actor) as conn:
        try:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability=minimum_capability(action),
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- an outage of the decision fails CLOSED
            logger.error("entity_types_mcp: access resolution failed: %s", exc)
            raise _tool_error("project_not_found", "Resource not found.") from exc
        if not decision.allowed or not decision.org_id:
            # Indistinguishable from absence, exactly like the REST door.
            raise _tool_error("project_not_found", "Resource not found.")
        try:
            result = run(conn, str(decision.org_id), actor)
        except EntityTypeExists as exc:
            # Before the generic conflict: the agent must be able to tell a
            # real duplicate from a replay, and the code is how.
            raise _tool_error("entity_type_exists", str(exc)) from exc
        except MasterDataNotFound as exc:
            raise _tool_error("not_found", str(exc)) from exc
        except MasterDataConflict as exc:
            raise _tool_error("conflict", str(exc)) from exc
        except MasterDataError as exc:
            # The shape refusals -- a malformed kind, an empty key. They travel
            # with their sentence: an agent told only "invalid" retries the
            # same call.
            raise _tool_error("invalid_declaration", str(exc)) from exc
        if action != "list":
            conn.commit()
    return result


def declare_entity_type(
    project_id: str,
    object_kind: str,
    canonical_key: str,
    display_name: str,
    idempotency_key: str = "",
):
    """Declare un type d'entite gouverne : son nom, sa cle canonique, son libelle.

    Aucune source requise : le type existe comme configuration avant
    d'etre alimente. Rejouer la MEME declaration (meme nom, meme cle, meme
    libelle) rend l'existant sans erreur ; une declaration DIFFERENTE du
    meme nom est refusee (`entity_type_exists`, avec le detenteur).
    """
    from core.object_kind_registry import declare_entity_type as _declare  # noqa: PLC0415

    result = _run(
        "declare",
        project_id,
        lambda conn, org_id, actor: _declare(
            conn,
            org_id=org_id,
            project_id=project_id,
            object_kind=object_kind,
            canonical_key=canonical_key,
            display_name=display_name,
            actor=actor,
        ),
        idempotency_key=idempotency_key,
    )
    registry = result["registry"]
    text = (
        f"{object_kind} deja declare tel quel -- aucun changement"
        if result["replayed"]
        else f"{object_kind} declare -- cle {canonical_key}"
    )
    return _envelope(
        text,
        {
            "registry_id": registry["id"],
            "object_kind": registry["object_kind"],
            "canonical_key": registry["canonical_key"],
            "display_name": registry["label"],
            "version_scope": registry["version_scope"],
            "lifecycle_state": registry["lifecycle_state"],
            "replayed": result["replayed"],
        },
    )


def list_entity_types(project_id: str):
    """Liste les types d'entite declares du projet, avec leur etat d'alimentation.

    `live_source_count` a zero signifie "declare, pas encore alimente" --
    un etat distinct de "jamais declare".
    """
    from core.object_kind_registry import list_entity_types as _list  # noqa: PLC0415

    types = _run(
        "list",
        project_id,
        lambda conn, org_id, actor: _list(conn, project_id=project_id),
    )
    return _envelope(
        f"{len(types)} type(s) d'entite declare(s)",
        {"entity_types": types, "entity_type_count": len(types)},
    )


def register(mcp) -> None:
    """Register the entity-type tools under the governance profile.

    Two tools and not one: a read that asked for a confirmation would be a lie
    in the catalogue, and a declaration that asked for none would be a worse
    one -- the reasoning `object_kind_mcp.register` states for its own three.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        declare_entity_type,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        # `host`, the neighbour's reasoning verbatim: declaring mints a governed
        # owner in a Project -- the host asks before it runs.
        confirmation_mode="host",
    )
    register_profiled(
        mcp,
        list_entity_types,
        profile="governance",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
