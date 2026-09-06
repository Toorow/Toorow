"""The entity reconciliation context over MCP -- the model's discovery door (68.7).

WHAT THIS TOOL ANSWERS. Before crossing anything, an LLM agent asks: what does
this Project declare (entity types, 68.1), which mapping columns designate
those types (bindings, 68.2), which rule-set versions derive their
classifications (68.6), and what coverage the governed matching reaches
(68.3). One question, one read:
``core.object_kind_registry.describe_entity_reconciliation_context``.

ONE WRITER, TWO DOORS. The console REST route
(``core.entity_context_api``) serves the SAME function; this module owns no
assembly and no shaping, so the model and the screen cannot drift (AC3).

THE GUARD IS THE NEIGHBOUR'S. ``resolve_strict_resource_access`` with
deny-by-default answering ``project_not_found`` rather than ``forbidden`` --
the existence of another Project's master data is itself sensitive, and an
outage of the decision fails CLOSED onto the same answer (the reasoning
``entity_types_mcp`` states verbatim for its own doors).

UNAVAILABLE IS NOT EMPTY. A dependency that has not landed (68.3's matching)
arrives as a section ``unavailable`` with its named reason, never as a count
of zero; a store that cannot be read is a ToolError
``entity_context_unavailable``, never an empty payload.

NOTHING HERE KNOWS WHAT A VIDEO IS. ``object_kind`` travels as an opaque
string, exactly as the generic core treats it (AD-2).
"""

from __future__ import annotations

import json
import logging

from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)


def _tool_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def list_entity_reconciliation_context(project_id: str) -> dict:
    """Ce que ce projet declare et ce que le rapprochement peut croiser.

    Les types d'entite declares (avec leur etat d'alimentation), les colonnes
    de mapping qui les designent, les versions de rule sets publiees qui
    derivent leurs classifications, et la couverture du matching gouverne.
    Chaque section est `ok` avec ses compteurs ou `unavailable` avec sa raison
    nommee -- jamais un zero qui n'a pas ete mesure. Les groupes non attaches
    (colonnes candidates non liees, occurrences sans verdict) sont nommes
    avec leurs compteurs.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.object_kind_registry import (  # noqa: PLC0415
        describe_entity_reconciliation_context,
    )
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    identity = _identity()
    if not identity or identity == "anonymous":
        raise _tool_error("project_not_found", "Resource not found.")

    with request_connection(identity) as conn:
        try:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability="view",
            )
        except Exception as exc:  # noqa: BLE001 -- an outage of the decision fails CLOSED
            logger.error("entity_context_mcp: access resolution failed: %s", exc)
            raise _tool_error("project_not_found", "Resource not found.") from exc
        if not decision.allowed or not decision.org_id:
            # Indistinguishable from absence, exactly like the REST door.
            raise _tool_error("project_not_found", "Resource not found.")
        try:
            return describe_entity_reconciliation_context(conn, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 -- "I could not look" is not "nothing"
            logger.error(
                "entity_context_mcp: read failed project=%s: %s", project_id, exc
            )
            raise _tool_error(
                "entity_context_unavailable",
                "The entity reconciliation context could not be read. "
                "This is not a count of zero.",
            ) from exc


def register(mcp) -> None:
    """Register the discovery tool -- an insights read, no confirmation.

    A read that asked for a confirmation would be a lie in the catalogue (the
    reasoning `entity_types_mcp.register` states for its own pair); an
    `insights` profile because discovering what a Project declares is the
    safest door this repository opens.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        list_entity_reconciliation_context,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
