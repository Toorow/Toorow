"""Story 38.15 -- Operate inbound delivery from MCP, on the console's own reads.

THE DESIGN IS THE ABSENCE OF A COMMIT TOOL, and that is deliberate rather than
unfinished. AC4 says a noninteractive host may inspect and prepare but must
receive an authenticated console continuation for authorization. The strongest
way to satisfy that is not to guard a commit tool -- it is not to have one. A
model cannot misuse a tool that does not exist, and no future prompt can talk it
into one.

So: six tools, all reads and one preparation. The reprocess PROPOSAL is
available here (it writes nothing and it is exactly what a model is good at
assembling); the reprocess EXECUTION is not, and every proposal carries the
console reference that completes it.

EVERY TOOL CALLS THE FUNCTION THE REST HANDLER CALLS. Not a similar query, not a
tuned projection -- ``get_inbound_health``, ``get_attachment_inbox``,
``get_delivery_timeline``, ``get_mapping_repair_context``,
``prepare_reprocess``, ``run_datastream_routing_test``. AC2 asks for semantically
identical state between console and MCP, and the only way two surfaces cannot
drift is if there is one implementation. The redaction lives in those functions
too, so MCP cannot accidentally expose a field the console excluded (AC3).

AD-5 SCOPING: every tool resolves the caller identity from the access token and
refuses a Datastream outside its readable projects. A refusal returns the same
shape as an absent resource, so a caller cannot enumerate other tenants'
Datastreams by watching which ones answer differently.

ASCII-only source (AI-03). Registered from ``core.main`` via
``register_inbound_tools``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The connector this surface operates. A name, never a vendor (AD-2).
_CONNECTOR_NAME = "inbound-managed-files"


def _environment() -> str:
    import os  # noqa: PLC0415

    return os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"


def _caller_identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _denied(reason: str) -> dict[str, Any]:
    """The one shape every refusal returns.

    Identical for "you may not" and "it does not exist": distinguishing them
    would let a caller enumerate other tenants' Datastreams by comparing
    answers (E38-NFR01).
    """
    return {
        "available": False,
        "reason": reason,
        "detail": (
            "No inbound resource is readable under this identity for the given "
            "Datastream."
        ),
    }


class _Scope:
    """An open connection, its Project, and the manager that knows how to close it.

    IT UNPACKS AS TWO, and that is the whole point. `__iter__` yields only the
    connection and the Project, so the six call sites read it exactly as they
    read the tuple it replaces -- and so does every test that stubs
    `_readable_datastream` with a plain `(conn, project_id)`. Making it a
    three-field NamedTuple broke four of those stubs immediately: an internal
    change that forces every fake to grow a field is a change that leaked.
    """

    __slots__ = ("conn", "project_id", "manager")

    def __init__(self, conn: Any, project_id: str, manager: Any = None) -> None:
        self.conn = conn
        self.project_id = project_id
        self.manager = manager

    def __iter__(self):
        return iter((self.conn, self.project_id))


def _close_scope(scope: Any) -> None:
    """Close what `_readable_datastream` handed out, whatever shape it is.

    A `_Scope` closes through its MANAGER -- the only thing that knows what
    closing means. A plain `(conn, project_id)` tuple, which is what the tests
    hand in, closes its connection. Neither is asked to know about the other.
    """
    manager = getattr(scope, "manager", None)
    if manager is not None:
        _release(manager)
        return
    conn = getattr(scope, "conn", None)
    if conn is None and isinstance(scope, tuple) and scope:
        conn = scope[0]
    if conn is not None:
        _close_quietly(conn)


def _close_quietly(closable: Any) -> None:
    try:
        closable.close()
    except Exception:  # noqa: BLE001, S110 -- nothing useful to do while closing
        pass


def _release(manager: Any) -> None:
    """Exit a connection manager -- the only thing that knows what closing means."""
    try:
        manager.__exit__(None, None, None)
    except Exception:  # noqa: BLE001, S110
        pass


def _readable_datastream(datastream_id: str) -> _Scope | None:
    """Open a connection ONLY if the caller may read this Datastream's project.

    Returns a ``_Scope`` -- which unpacks as ``(conn, project_id)`` -- or None.
    The caller closes it with ``scope.close()``. Fails closed on any error: an
    access check that could not run has not granted access.

    THE MANAGER IS HELD, NOT DISCARDED. This read
    `_core_db.get_connection().__enter__()`, which drops the manager on the
    floor: its generator stays suspended at the `yield` until the garbage
    collector finalizes it, and only then does its `finally` run -- closing a
    connection the callers had already closed by hand, at a moment nothing
    controls. Harmless only for as long as that `finally` does nothing but
    `conn.close()`; the day it also releases a pool slot or clears an access
    context, all six callers skip it in silence.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.project_access import identity_can_read_project  # noqa: PLC0415

    identity = _caller_identity()
    conn = None
    manager = _core_db.request_connection(identity)
    try:
        conn = manager.__enter__()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
        if row is None or not row[0]:
            _release(manager)
            return None
        project_id = row[0]
        if not identity_can_read_project(project_id, identity, conn):
            _release(manager)
            return None
        return _Scope(conn, str(project_id), manager)
    except Exception as exc:  # noqa: BLE001 -- fail closed, never leak the cause
        logger.debug(
            "inbound_mcp: datastream scope check skipped: %s", type(exc).__name__
        )
        _release(manager)
        return None


def _console_reference(datastream_id: str, *, section: str) -> dict[str, Any]:
    """A semantic console destination, never a hard-coded URL.

    UNE SEULE FORME, DEUX SURFACES. This shape used to be written out here and
    nowhere else, and then `inbound_health` needed the same thing to link an
    alert to its repair (AC4). Two copies would be two chances to drift, and the
    drift is invisible: `ContentRouter` drops a reference it cannot read in
    SILENCE, so a stale copy reads like a working link.

    The shape itself was proved against the router the hard way -- three errors
    at once, each sufficient to have the reference thrown away: `surface:
    "workspace"` where the contract admits only `project` or `global`; `object`
    where the key read is `object_type`; and `section: "imports"`, which
    declares only `type: "import"` and so cannot hold a Datastream at all. That
    history now lives with the single definition, in `inbound_health`.
    """
    from core.inbound_health import console_reference  # noqa: PLC0415

    return console_reference(datastream_id, tab=section)


# ---------------------------------------------------------------------------
# The tools.
# ---------------------------------------------------------------------------


def get_inbound_health(datastream_id: str = "") -> dict[str, Any]:
    """Layered health of the inbound connector: installation, domain, activation,
    delivery, data.

    Pass a ``datastream_id`` to include the tenant-scoped layers and counters.
    Each layer names the AUTHORITY that can repair it, so an answer says who
    acts and not only that something is wrong.

    Returns counts, states and timestamps only -- no credential, no signing
    secret, no recipient address, no row content.
    """
    from core.inbound_health import get_inbound_health as _read  # noqa: PLC0415

    datastream_id = (datastream_id or "").strip()

    if not datastream_id:
        from core import db as _core_db  # noqa: PLC0415

        try:
            with _core_db.get_connection() as conn:
                return _read(
                    conn,
                    connector_name=_CONNECTOR_NAME,
                    environment=_environment(),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("inbound_mcp: health read skipped: %s", type(exc).__name__)
            return _denied("health_unavailable")

    scope = _readable_datastream(datastream_id)
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.org_id FROM app.datastreams d "
                "JOIN app.projects p ON p.id = d.project_id WHERE d.id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
        return _read(
            conn,
            connector_name=_CONNECTOR_NAME,
            environment=_environment(),
            datastream_id=datastream_id,
            org_id=row[0] if row else None,
        )
    finally:
        _close_scope(scope)


def list_inbound_attachments(
    datastream_id: str, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """One entry PER ATTACHMENT received by this Datastream, newest first.

    Includes the deliveries that never landed -- rejected, failed, denied --
    because those are the ones an operator is looking for. Each entry carries
    the redacted scan verdict, so the reason a file was refused is readable
    without downloading a byte of it.
    """
    scope = _readable_datastream((datastream_id or "").strip())
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        from core.inbound_health import get_attachment_inbox  # noqa: PLC0415

        # UNE COMMANDE, UNE REPONSE. Ce tour bornait l'entree en silence
        # (`max(1, min(..., 200))`) la ou la porte REST laisse le lecteur refuser
        # et rend 400 `invalid_param` -- meme appel, deux issues, selon la porte
        # empruntee. Le lecteur EST l'autorite sur ses bornes ; macher l'entree a
        # sa place, c'est servir 200 lignes a qui en a demande 500 sans le lui
        # dire. Les valeurs passent telles quelles et le refus est nomme.
        items = get_attachment_inbox(
            conn,
            datastream_id=datastream_id.strip(),
            limit=limit,
            offset=offset,
        )
        return {"items": items, "count": len(items)}
    except ValueError as exc:
        # Les bornes de page sont l'erreur de l'appelant, et le dire est utile --
        # la meme phrase que celle de la porte REST.
        logger.debug("inbound_mcp: inbox bounds refused: %s", exc)
        return _denied("invalid_param")
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbound_mcp: inbox read skipped: %s", type(exc).__name__)
        return _denied("inbox_unavailable")
    finally:
        _close_scope(scope)


def get_inbound_delivery(datastream_id: str, receipt_id: str) -> dict[str, Any]:
    """One delivery and every attachment it carried, with each one's outcome.

    States explicitly whether published data changed, rather than leaving that
    to be inferred from an outcome string.
    """
    scope = _readable_datastream((datastream_id or "").strip())
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        from core.inbound_health import get_delivery_timeline  # noqa: PLC0415

        timeline = get_delivery_timeline(
            conn,
            receipt_id=(receipt_id or "").strip(),
            datastream_id=datastream_id.strip(),
        )
        if timeline is None:
            return _denied("not_readable")
        return timeline
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbound_mcp: delivery read skipped: %s", type(exc).__name__)
        return _denied("delivery_unavailable")
    finally:
        _close_scope(scope)


def prepare_inbound_reprocess(
    datastream_id: str, raw_import_id: str, target_mapping_version_id: str = ""
) -> dict[str, Any]:
    """Prepare -- never commit -- a reprocess of one retained file.

    Reports whether the retained bytes can still be reprocessed (retention,
    readability, integrity), which versions would be bound, and what the
    rollback is. It writes NOTHING.

    ``target_mapping_version_id`` selects WHICH version the file would be
    replayed under (38.18 AC1). Empty means the Datastream's current pair. The
    proposal lists the alternatives in ``available_versions``, because a choice
    with no list is a parameter and not a choice -- a host would have to already
    know a version id to name one.

    There is deliberately no MCP tool that executes a reprocess. Executing can
    move the published pointer, so it requires a confirmation this channel
    cannot verify; the ``console`` reference returned here is where it
    completes.
    """
    scope = _readable_datastream((datastream_id or "").strip())
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        from core.inbound_reprocess import prepare_reprocess  # noqa: PLC0415

        proposal = prepare_reprocess(
            conn,
            raw_import_id=(raw_import_id or "").strip(),
            datastream_id=datastream_id.strip(),
            target_mapping_version_id=(target_mapping_version_id or "").strip() or None,
        )
        proposal["commit_available_here"] = False
        proposal["console"] = _console_reference(
            # `data` est l'onglet ou les arrivees d'un Datastream se lisent
            # (`ui/admin/src/shell/navigation.ts#WORKSPACES`). << imports >> n'en est pas un.
            datastream_id.strip(), section="data"
        )
        proposal["why_not_here"] = (
            "A reprocess can move the published pointer. It is confirmed in the "
            "toorow console, where the confirmation is server-verifiable."
        )
        return proposal
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbound_mcp: proposal skipped: %s", type(exc).__name__)
        return _denied("proposal_unavailable")
    finally:
        _close_scope(scope)


def prepare_inbound_reprocess_scope(
    datastream_id: str,
    raw_import_ids: list[str] | None = None,
    criterion: dict[str, Any] | None = None,
    target_mapping_version_id: str = "",
    limit: int = 0,
) -> dict[str, Any]:
    """Prepare -- never commit -- a reprocess of a NAMED SCOPE of retained files.

    AI-281. Story 38.18 delivered the governed scope to the REST route and left
    this channel with the unit replay only: an agent could replay ONE file and
    not a scope, so the AC5 promise of one command on both surfaces held for the
    piece and not for the set. The shared body is `_replay_one` either way; what
    was missing is the door.

    THE SAME PREPARE, AND THE SAME COUNT BEFORE THE ACT. `prepare_reprocess_scope`
    reads and re-hashes every candidate, and always ENUMERATES its members --
    a bare count would let a host say "12 deliveries" while nobody can tell WHICH
    twelve. That property belongs to the function, so this tool inherits it by
    calling it rather than by promising it.

    `raw_import_ids` OR `criterion`, never both: two selections in one request is
    an ambiguity, and resolving it silently either way replays a set nobody asked
    for. The refusal comes from the same validator the REST route uses.

    There is deliberately NO execute tool here, exactly as for the unit replay.
    Executing can move the published pointer, so it requires a confirmation this
    channel cannot verify; the `console` reference is where it completes.
    """
    scope = _readable_datastream((datastream_id or "").strip())
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        from core.inbound_reprocess import (  # noqa: PLC0415
            MAX_SCOPE_MEMBERS,
            prepare_reprocess_scope,
        )

        proposal = prepare_reprocess_scope(
            conn,
            datastream_id=datastream_id.strip(),
            raw_import_ids=list(raw_import_ids) if raw_import_ids else None,
            criterion=criterion or None,
            target_mapping_version_id=(target_mapping_version_id or "").strip() or None,
            # A host that names no bound gets the store's own, never an unbounded
            # read: the ceiling is the function's, not this door's to invent.
            limit=int(limit) if limit else MAX_SCOPE_MEMBERS,
        )
        proposal["commit_available_here"] = False
        proposal["console"] = _console_reference(datastream_id.strip(), section="data")
        proposal["why_not_here"] = (
            "A reprocess can move the published pointer. It is confirmed in the "
            "toorow console, where the confirmation is server-verifiable."
        )
        return proposal
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbound_mcp: scope proposal skipped: %s", type(exc).__name__)
        return _denied("proposal_unavailable")
    finally:
        _close_scope(scope)


def get_inbound_mapping_context(datastream_id: str, raw_import_id: str) -> dict[str, Any]:
    """What one retained file contained, what it is mapped against, how to change it.

    Story 38.16 AC5 asks that console and MCP use the SAME prepare command. The
    console reads `get_mapping_repair_context` through the REST handler; this
    tool calls that same function, so the two surfaces cannot drift and the
    redaction that lives in it applies here too.

    A READ, and it stays one. The answer's `governed_path` names the engine that
    changes a mapping (`datastream_change`) and the console URLs that run it --
    it does not run them. That is the same shape as `prepare_inbound_reprocess`:
    assemble the proposal here, complete it where the confirmation can be
    verified. Nothing in this module commits, and the name of this tool carries
    no forbidden verb so the epic-level guard keeps holding.
    """
    scope = _readable_datastream((datastream_id or "").strip())
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        from core.inbound_mapping_entry import get_mapping_repair_context  # noqa: PLC0415

        return get_mapping_repair_context(
            conn,
            raw_import_id=(raw_import_id or "").strip(),
            datastream_id=datastream_id.strip(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbound_mcp: mapping context skipped: %s", type(exc).__name__)
        return _denied("mapping_context_unavailable")
    finally:
        _close_scope(scope)


def test_inbound_routing(datastream_id: str, channel: str = "email") -> dict[str, Any]:
    """Would a delivery sent to this Datastream right now actually reach it?

    A synthetic test at DATASTREAM level (Story 38.15 AC5). The connector
    already had one at installation level -- it proves the front door. This
    proves the chain behind it: lifecycle, configured channel, verified domain,
    a still-valid credential, and whether that credential resolves BACK to this
    Datastream. Every link can break while the connector as a whole is healthy,
    and all of them fail identically from outside: nothing arrives.

    It creates NO DELIVERY EVIDENCE -- no receipt, no raw import, no execution,
    no publication -- so it can never be confused with a provider delivery, and
    the inbox never shows a phantom file an operator goes looking for. The
    result says ``synthetic: true`` in its own payload rather than leaving that
    to the caller's memory of which endpoint they used.

    IT DOES SPEND ONE RESOLUTION EVENT, and saying otherwise was a lie with a
    cost. This tool used to claim it wrote "NOTHING". It calls
    ``resolve_by_token_hash``, which records a rate-limit event
    (``inbound_credentials._record_resolution_rate_event``), and this module
    commits it. That is deliberate -- ``inbound_routing_test`` explains why
    re-deriving the lookup to dodge the throttle would duplicate security logic
    and report "routing works" for a Datastream that is over budget -- but the
    consequence is real: repeated tests consume the resolution budget, and
    ``_enforce_resolution_rate_limit`` then refuses REAL deliveries. An operator
    who is told nothing was written cannot know that.

    The report names the FIRST broken link and marks the rest ``not_reached``
    instead of failing them: a step nobody tried is not a step that failed, and
    reporting it as one sends an operator to repair something that may be fine.
    """
    scope = _readable_datastream((datastream_id or "").strip())
    if scope is None:
        return _denied("not_readable")
    conn, _project_id = scope
    try:
        from core.inbound_routing_test import (  # noqa: PLC0415
            RoutingTestValidationError,
            run_datastream_routing_test,
        )

        try:
            report = run_datastream_routing_test(
                conn, datastream_id=datastream_id.strip(), channel=channel
            )
        except RoutingTestValidationError:
            return _denied("unsupported_channel")
        # LE MEME APPEL QUE LA CONSOLE, pas un voisin. Les deux surfaces
        # partagent `run_datastream_routing_test`, donc elles ne peuvent pas
        # decrire le meme Datastream differemment (38.15 AC2).
        conn.commit()
        report["console"] = _console_reference(datastream_id.strip(), section="data")
        return report
    except Exception as exc:  # noqa: BLE001
        logger.debug("inbound_mcp: routing test skipped: %s", type(exc).__name__)
        return _denied("routing_test_unavailable")
    finally:
        _close_scope(scope)


def register_inbound_tools(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Register the seven inbound tools. Reads and two preparations; no commit.

    SEVEN since AI-281 added `prepare_inbound_reprocess_scope`: the governed
    SCOPE was reachable from the REST route only, so an agent could replay one
    piece and not a named set. Both preparations share `_replay_one` with the
    console; what was missing was the door.

    The count is pinned by
    `tests/conformance/test_datastream_readers_carry_project_scope.py`
    ::test_the_inbound_tuple_and_the_swept_surface_agree -- this docstring said
    "five" while six were registered and `INBOUND_MCP_TOOLS` declared six.

    Declared (AD-43). Five reads -- `test_inbound_routing` included: it "creates NO
    DELIVERY EVIDENCE -- no receipt, no raw import, no execution", which is what
    makes it a read rather than a synthetic write. `prepare_inbound_reprocess` is
    the AD-24 `prepare`: it reports whether retained bytes can still be
    reprocessed and commits nothing, so it authorizes nothing and needs no human.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_inbound_health,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        list_inbound_attachments,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_inbound_delivery,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_inbound_mapping_context,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        test_inbound_routing,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        prepare_inbound_reprocess,
        profile="operations",
        effect="prepare",
        data_class="operational",
        confirmation_mode="server",
    )
    register_profiled(
        mcp,
        prepare_inbound_reprocess_scope,
        profile="operations",
        effect="prepare",
        data_class="operational",
        confirmation_mode="server",
    )


#: The tools this module registers. Exported so a conformance test can assert
#: the list without importing FastMCP -- and, more importantly, so a commit tool
#: added here later fails a test rather than shipping quietly.
INBOUND_MCP_TOOLS: tuple[str, ...] = (
    "get_inbound_health",
    "list_inbound_attachments",
    "get_inbound_delivery",
    "get_inbound_mapping_context",
    "prepare_inbound_reprocess",
    "prepare_inbound_reprocess_scope",
    "test_inbound_routing",
)
