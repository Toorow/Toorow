"""The montage, from the model's side: who names a dimension, and declare it.

Jean, 2026-08-12: the examples were given so that ASSEMBLING the join is possible
in the console AND through the MCP, by anybody. A detection that only an engineer
can act on is not a feature; it is a report.

Two doors, and they are the two halves of one gesture:

  * ``describe_dimension_reference`` -- who names the values of this dimension in
    this Project, or the gap with the ONE gesture that closes it;
  * ``declare_dimension_reference`` -- set a Datastream's role to
    ``Reference & targets``, which is the half a person could not write at all
    until today: `data_role` was accepted at creation and dropped on every update.

WHAT THIS DOOR DELIBERATELY DOES NOT DO. It never binds a field to a canonical
target. That is the mapping, it is versioned, reviewed and already served by
``mapping_proposal_mcp`` -- and a second path writing bindings would put two
authorities on what a column means. When the role is declared and the binding is
still missing, the answer says so and names the mapping as the next step rather
than performing it silently.

NO VALUES TRAVEL THROUGH HERE. The reference is a shape -- which stream, which
column, which role -- so this tool answers identifiers and counts. The values
themselves obey the classification policy and are read through the surface that
enforces it (``unresolved_values``), never through a door whose contract says
provider rows never leave.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The role a stream must carry to be the one that NAMES a dimension. Mirrors
#: `core.datastreams.DATA_ROLES`, and the tests assert the two agree: a word this
#: tool would accept and the store would refuse must not exist.
REFERENCE_ROLE = "Reference & targets"


def describe_dimension_reference(project_id: str, dimension: str):
    """Say which Datastream NAMES the values of a dimension, or name what is missing.

    - `dimension` is the canonical target that mapping versions bind to
      (`video`, `campaign_id`...), never one source's column name.
    - Answers `state='declared'` with the reference stream and ITS column, or
      `state='absent'` with a `gap` and the sentence of the single gesture
      that closes it.

    The join is DERIVED: two streams binding a field to the same canonical
    target are talking about the same thing, and `data_role` says which of the
    two names it. Nothing is stored in a second table.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.dimension_reference import read_reference  # noqa: PLC0415
    from core.mcp_scope import refuse_unless_project_scope  # noqa: PLC0415

    identity = _identity()
    refuse_unless_project_scope(project_id, identity)
    if not str(dimension or "").strip():
        return {
            "error": "missing_dimension",
            "message": "dimension is required: the canonical target to resolve.",
        }
    try:
        with request_connection(identity) as conn:
            answer = read_reference(
                conn, project_id=project_id, canonical_dimension=dimension
            )
    except Exception as exc:  # noqa: BLE001 -- unreadable is not "none declared"
        return {"error": "reference_unavailable", "message": str(exc)}
    return {"project_id": project_id, **answer}


def declare_dimension_reference(project_id: str, datastream_id: str):
    """Declare this Datastream as the REFERENCE: it names, it does not measure.

    Writes `data_role='Reference & targets'` on this stream. This is the half
    of the montage nobody could write: the role was accepted at creation and
    silently ignored on every update.

    It binds NO field. When the stream binds nothing to the dimension yet, the
    answer says so and points at the mapping, which is versioned and reviewed --
    two write paths onto what a column means would be two authorities.
    """
    from core.datastreams import get_datastream  # noqa: PLC0415
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import refuse_unless_project_scope  # noqa: PLC0415

    identity = _identity()
    refuse_unless_project_scope(project_id, identity, minimum_capability="edit")
    try:
        with request_connection(identity) as conn:
            existing = get_datastream(datastream_id, project_id, conn)
            if existing is None:
                # Existence-hiding: a Datastream of another project answers
                # exactly like one that does not exist.
                return {
                    "error": "not_found",
                    "message": "This Datastream does not exist in this project.",
                }
            if existing.get("data_role") == REFERENCE_ROLE:
                # Idempotent, and it SAYS it changed nothing rather than
                # reporting a write that did not happen.
                return {
                    "datastream_id": datastream_id,
                    "data_role": REFERENCE_ROLE,
                    "changed": False,
                    "previous_role": REFERENCE_ROLE,
                }
            previous = existing.get("data_role")
            # ONE GOVERNED WRITER FOR THE ROLE (2026-08-31, amendment 9).
            # This tool wrote through `update_datastream` directly, so two
            # doors moved `data_role` under two different rules -- and this
            # one took no row lock, so a concurrent change from the console
            # was arbitrated by whoever committed last. The base it states is
            # the role it JUST read, three lines above, which is exactly the
            # claim the governed door asks every caller to make.
            from core.datastream_data_role import (  # noqa: PLC0415
                DataRoleChangeRefused,
                change_data_role,
            )

            try:
                change_data_role(
                    conn,
                    project_id=project_id,
                    datastream_id=datastream_id,
                    proposed=REFERENCE_ROLE,
                    expected=previous or "",
                    actor=identity,
                )
            except DataRoleChangeRefused as refusal:
                return {"error": refusal.code, "message": str(refusal)}
            updated = get_datastream(datastream_id, project_id, conn)
            conn.commit()
    except ValueError as exc:
        return {"error": "invalid_role", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": "declaration_failed", "message": str(exc)}
    return {
        "datastream_id": datastream_id,
        "data_role": (updated or {}).get("data_role"),
        "changed": True,
        "previous_role": previous,
        "next_step": (
            "Bind the column that carries the identity to its canonical target "
            "on this Datastream's mapping, so the join can be derived."
        ),
    }


def register(mcp) -> None:
    """Register the two reference tools.

    The declaration is a `confirmed_write`: it changes which stream the product
    believes is the authority on a vocabulary, so every unresolved count of that
    dimension changes with it. `data_class` is `operational` -- a role and a
    stream id, no personal datum, no provider row.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp, describe_dimension_reference,
        profile="governance", effect="read",
        data_class="operational", confirmation_mode="none",
    )
    register_profiled(
        mcp, declare_dimension_reference,
        profile="governance", effect="confirmed_write",
        data_class="operational", confirmation_mode="human",
    )


def _identity() -> str:
    """The caller, resolved exactly as every other tool resolves it."""
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"
