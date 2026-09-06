"""Server-side attestation and revocation of an MCP host capability context.

THE DEFECT THIS CLOSES. `mcp_profiles.visible_profiles` decided whether a host
may see Operations/Governance/Support from three fields -- `enabled_profiles`,
`endpoint_binding`, `workspace_evidence_hash` -- read straight out of the token
claims and only SHAPE-checked. The row those fields are supposed to come from,
`app.mcp_capability_contexts`, was written once by `host_preflight.bind_host_
connection` and then read by no middleware at all. The compensation was a global
env flag, `TOOROW_MCP_HIGHRISK_ENABLED`, defaulting OFF: with it off every
governed MCP write was unreachable on every honest deployment; with it on, all
of them opened on the host's word. Named by
`reviews/audit-2026-08-17/10-mcp-app.md:30,100` and by the C1 comment that
described two possible architectures and chose neither.

THE ARCHITECTURE CHOSEN, AND WHY. Two were open (audit `:109`): mint the grants
into the token at the auth server from the table, or VERIFY them against the
table at every call. This module is the second.

A minted claim is a photograph of the row at issue time. Revoking the row would
then leave every already-minted token carrying the old grants until it expired,
and "revoke" would mean "stop issuing", not "cut". That is exactly the defect
`session_revocation` (67-15d, commit 82726819) closed for the browser ticket,
which itself copied `render_shares.resolve_session`: the LIVE state is
revalidated at every call, so a revocation is felt at the NEXT call rather than
at expiry. Three surfaces, one rule.

The cost is one indexed probe per MCP call -- the same order as the browser
session probe -- against `mcp_capability_contexts_live_endpoint` (migration
283). The token keeps only a POINTER (`capability_context_id`, or the
`endpoint_binding` it was bound to); every grant that decides visibility is read
from the row.

FAILING CLOSED, EVERYWHERE. No pointer, no row, a revoked row, an unreadable
store: the caller gets Insights and nothing else. `visible_profiles` refuses to
raise a caller above Insights without `attested_context_id`, which only this
module can set, so a fabricated claim cannot manufacture one.

WHAT REVOCATION MEANS HERE (audit open question n°3). It cuts the capabilities,
at the next call, for every session already running on that context -- the same
answer `render_shares` gives, for the same reason: the living state is
revalidated at the call. The MCP session itself is not torn down (nothing in the
MCP wire lets a server do that), and it does not need to be: the session keeps
working at the Insights floor, and every high-risk tool disappears from both
discovery and invocation. The row is never deleted, so the screen can still say
what was cut, when, and by whom.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationSpec, execute_operation

logger = logging.getLogger(__name__)

#: The four grant fields `mcp_profiles` decides visibility from. Read from the
#: row, never from a claim.
_ATTESTED_COLUMNS = (
    "id",
    "org_id",
    "host",
    "workspace_id",
    "workspace_type",
    "client_id",
    "endpoint_binding",
    "enabled_profiles",
    "workspace_evidence_hash",
    "interactive_presence_evidence_hash",
    "policy_version",
    "catalog_version",
    "created_at",
    "revoked_at",
    "revoked_by",
    "revocation_reason",
)

_SELECT = "SELECT " + ",".join(_ATTESTED_COLUMNS) + " FROM app.mcp_capability_contexts "


class CapabilityContextUnavailable(Exception):
    """No such capability context (or it is already revoked)."""


def _row_to_context(row) -> dict[str, Any]:
    context = dict(zip(_ATTESTED_COLUMNS, row, strict=True))
    profiles = context.get("enabled_profiles")
    if isinstance(profiles, str):  # psycopg may hand back raw jsonb text
        try:
            profiles = json.loads(profiles)
        except ValueError:
            profiles = []
    context["enabled_profiles"] = profiles if isinstance(profiles, list) else []
    for key in ("created_at", "revoked_at"):
        value = context.get(key)
        context[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return context


def attest_capability_context(
    conn,
    *,
    context_id: str | None = None,
    endpoint_binding: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the LIVE capability-context row a caller points at, or ``None``.

    THE THIRD KEY, 2026-09-04 (`mcp-tool-surface.md`, amendment of that day): a
    token this deployment verifies is a Google ID token, which carries no
    ``capability_context`` claim and never will. What it does carry, signed, is
    the audience it was issued for (the endpoint the token is valid for) and its
    subject (the principal). ``(endpoint_binding, client_id)`` resolves a live
    row by that pair, and by that pair only when EXACTLY ONE live row carries it
    -- two bindings for one principal on one endpoint are an ambiguity, refused
    like the endpoint-only fallback below. Nothing of the grants is read from the
    token: the row still carries them, at this call, revocation included.

    Resolution order is deliberate. ``context_id`` is the primary key and is
    unambiguous, so it wins. ``endpoint_binding`` is only a fallback -- the index
    on it is NOT unique (migration 067 keys it with ``org_id``), so it resolves
    only when EXACTLY ONE live row carries it; two live rows on one endpoint is
    an ambiguity that must not be resolved by picking one, and returns ``None``.

    A revoked row is not a row here: the ``WHERE revoked_at IS NULL`` is what
    makes the revocation bite at the next call.
    """
    if context_id:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT + "WHERE id=%s AND revoked_at IS NULL",
                (context_id,),
            )
            row = cur.fetchone()
        return _row_to_context(row) if row is not None else None
    if endpoint_binding and client_id:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT
                + "WHERE endpoint_binding=%s AND client_id=%s AND revoked_at IS NULL LIMIT 2",
                (endpoint_binding, client_id),
            )
            rows = cur.fetchall()
        if len(rows) == 1:
            return _row_to_context(rows[0])
        if len(rows) > 1:
            logger.warning(
                "mcp_attestation: (endpoint, principal) resolves to several live contexts; "
                "refusing to choose one"
            )
        return None
    if endpoint_binding:
        with conn.cursor() as cur:
            cur.execute(
                _SELECT + "WHERE endpoint_binding=%s AND revoked_at IS NULL LIMIT 2",
                (endpoint_binding,),
            )
            rows = cur.fetchall()
        if len(rows) == 1:
            return _row_to_context(rows[0])
        if len(rows) > 1:
            logger.warning(
                "mcp_attestation: endpoint binding resolves to several live contexts; "
                "refusing to choose one"
            )
        return None
    return None


#: The preflight that produced a context, so the list can date the negotiation
#: rather than only the binding. LEFT JOIN: a context whose preflight was purged
#: is still a context, and dropping it from the list would hide a live grant.
_PREFLIGHT_COLUMNS = ("preflight_id", "preflight_host_key", "preflight_state", "preflight_dated_at")

_LIST_SELECT = (
    "SELECT "
    + ",".join(f"c.{name}" for name in _ATTESTED_COLUMNS)
    + ",p.id,p.host_key,p.state,p.dated_at "
    "FROM app.mcp_capability_contexts c "
    "LEFT JOIN app.host_preflights p ON p.capability_context_id = c.id "
)


def list_capability_contexts(conn, *, org_id: str, include_revoked: bool = True) -> list[dict]:
    """Every capability context of one organization, newest first, with its preflight.

    Revoked rows are INCLUDED by default: a lifecycle screen that hides what was
    cut cannot answer "was this host ever connected, and who cut it".

    The dated preflight travels with the context because they answer one
    question in two halves -- what was negotiated, and what was granted. Two
    reads would let a screen show one without the other.
    """
    clause = (
        "WHERE c.org_id=%s" if include_revoked else "WHERE c.org_id=%s AND c.revoked_at IS NULL"
    )
    with conn.cursor() as cur:
        cur.execute(_LIST_SELECT + clause + " ORDER BY c.created_at DESC", (org_id,))
        rows = cur.fetchall()
    listed = []
    for row in rows:
        context = _row_to_context(row[: len(_ATTESTED_COLUMNS)])
        context.update(dict(zip(_PREFLIGHT_COLUMNS, row[len(_ATTESTED_COLUMNS) :], strict=True)))
        listed.append(context)
    return listed


def read_capability_context(conn, *, context_id: str) -> dict[str, Any]:
    """Read one context REGARDLESS of revocation; raise when it does not exist."""
    with conn.cursor() as cur:
        cur.execute(_SELECT + "WHERE id=%s", (context_id,))
        row = cur.fetchone()
    if row is None:
        raise CapabilityContextUnavailable("capability context unavailable")
    return _row_to_context(row)


def revoke_capability_context(
    conn,
    *,
    context_id: str,
    reason: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Cut one capability context. The next MCP call on it sees Insights only.

    Routes through ``operations.execute_operation`` like every other write of
    this lifecycle (E36-NFR05): the audit row and the outbox event are written in
    the same transaction as the cut, so a revocation can never be invisible.

    Idempotent by design at the SQL level -- the UPDATE is guarded by
    ``revoked_at IS NULL``, so re-revoking an already-cut context returns its
    existing cut rather than moving the date. Un-revoking is refused by the
    trigger of migration 283, so there is no path back.
    """
    existing = read_capability_context(conn, context_id=context_id)
    org_id = existing["org_id"]
    revoked_reason = (reason or "").strip() or "revoked by an operator"

    spec = OperationSpec(
        command_type="mcp.capability_context.revoke",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"capability-context:{context_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": existing.get("policy_version") or "",
            "catalog": existing.get("catalog_version") or "",
            "tool": "rest-v1",
        },
        request_payload={
            "context_id": context_id,
            "endpoint_binding": existing.get("endpoint_binding"),
            "enabled_profiles": existing.get("enabled_profiles"),
            "reason": revoked_reason,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"capability-context:{context_id}:revoke",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.mcp_capability_contexts "
                "SET revoked_at=NOW(),revoked_by=%s,revocation_reason=%s "
                "WHERE id=%s AND revoked_at IS NULL",
                (actor, revoked_reason, context_id),
            )
        after = read_capability_context(operation_conn, context_id=context_id)
        result = {
            "capability_context_id": context_id,
            "org_id": org_id,
            "endpoint_binding": after.get("endpoint_binding"),
            "revoked_at": after.get("revoked_at"),
            "revoked_by": after.get("revoked_by"),
            "revocation_reason": after.get("revocation_reason"),
            "enabled_profiles_after_revocation": ["insights"],
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"revoked_at": existing.get("revoked_at")}),
            after_hash=_canonical_hash({"revoked_at": after.get("revoked_at")}),
            result=result,
            outbox_payload={
                "capability_context_id": context_id,
                "transition": "revoked",
                "operation_id": operation_id,
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    return operation.result


def _canonical_hash(payload: dict[str, Any]) -> str:
    import hashlib  # noqa: PLC0415

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mint_context_id() -> str:
    """The one place a capability-context identifier is shaped."""
    return f"mcpctx_{ULID()}"


__all__ = [
    "CapabilityContextUnavailable",
    "attest_capability_context",
    "list_capability_contexts",
    "mint_context_id",
    "read_capability_context",
    "revoke_capability_context",
]
