"""Story 36.14 -- capability-driven host preflight, install handoff and binding.

An onboarding operator chooses an MCP Apps host from a *maintained capability
catalog* and Toorow builds a dated, capability-negotiated setup plan for it. When
the operator lacks host-admin authority the install is handed to the correct
administrator via a minimal purpose-scoped handoff (no Toorow data). On a verified
install/authorization the connection is BOUND: a ``app.mcp_capability_contexts``
row (Story 36.11 / migration 067) pins the endpoint profile, organization,
host/workspace policy and catalog version. When app UI is unsupported the plan
falls back to a standard-tool path with mandatory bounded evidence (console
deep-links are additional help only). When policy/capabilities change a stale
preflight is invalidated and high-risk profiles stay hidden until re-approved.

INVARIANTS (the 36.14 review is adversarial on these):

  * E36-NFR03 -- NO BRAND-FIRST ORDERING. Hosts are OPAQUE entries keyed by
    capability. The catalog is a source-agnostic data structure; two hosts that
    differ only by name receive identical treatment. No Claude-first / ChatGPT-
    first sequence is ever encoded; ordering (when a UI needs one) is purely
    capability/plan/role driven, host name is opaque display data.

  * HIGH-RISK PROFILES BIND ONLY WITH VERIFIABLE WORKSPACE PROOF. A capability
    context is written with ``enabled_profiles`` above ``insights`` ONLY when a
    64-hex ``workspace_evidence_hash`` is supplied AND the host entry requires
    (and the caller supplied) that proof. Absent proof we fail closed: only
    ``insights`` is bound and ``mcp_profiles.visible_profiles`` keeps every
    high-risk profile hidden regardless of any host self-report.

  * MINIMAL PURPOSE-SCOPED HANDOFF -- reuses ``setup_responsibilities.prepare_handoff``
    which reveals only the bound install action, expiry and return condition. NO
    Toorow data, no report, no token, no secret is ever placed in the handoff.

  * SERVER-AUTHORITATIVE RECONCILIATION -- the callback state is reconciled from
    SERVER evidence via ``setup_responsibilities.reconcile_task_state`` (the
    ``host_connected`` return condition), never trusted from a client callback.

  * E36-NFR05 -- REUSE EXISTING SEAMS. Every write routes through
    ``operations.execute_operation`` (atomic audit + outbox). The catalog version
    is read from ``mcp_profiles.catalog_version``; the binding row is the 36.11
    context consumed by ``visible_profiles``/the capability middleware. This module
    duplicates no REST business rule and adds no provider/source vocabulary.

Mirrors ``source_delegation`` conventions: ``from __future__ import annotations``,
lazy imports of the shared seams, a state machine driven only through the operation
seam, French user-facing microcopy, ASCII-only source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationResult, OperationSpec, execute_operation

# Preflight lifecycle. The install/authorization machinery (OAuth-style) reuses the
# handoff carrier + callback reconciliation; here we own the source-agnostic state
# of the preflight itself.
PREFLIGHT_STATES = {"prepared", "handed_off", "installed", "bound", "stale", "failed"}
TERMINAL_STATES = {"bound", "failed"}

# The high-risk profiles that may only be bound with server-verifiable workspace
# proof. Kept in sync with mcp_profiles._HIGH_RISK_PROFILES (fail closed if absent).
_HIGH_RISK_PROFILES: frozenset[str] = frozenset({"operations", "governance", "support"})
_DEFAULT_PROFILE = "insights"

# The setup task the preflight binds to (derive_initial_tasks step_key).
_HOST_TASK_STEP_KEY = "host_connection"
_HOST_RETURN_KIND = "host_connected"


class HostPreflightValidationError(ValueError):
    """Raised before SQL when preflight inputs are unsafe or malformed."""


class HostPreflightConflict(RuntimeError):
    """The preflight cannot transition from its current state."""


class HostPreflightUnavailable(RuntimeError):
    """The preflight or its host entry does not exist or is unusable."""


# ---------------------------------------------------------------------------
# The maintained host capability catalog (E36-NFR03: opaque, capability-keyed).
#
# Entries are DATED and capability-driven because host plans, permissions and
# tool-catalog refresh behaviour are volatile (epic constraint). A host is an
# opaque entry: ``host_key`` is a stable identifier, ``display_name`` is display-
# only data and NEVER participates in any ordering or gating decision. The catalog
# is deliberately a flat mapping (dict) -- there is NO ordered "preferred host"
# list anywhere. When a surface needs an order it sorts by capability/plan/role.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostCapabilityEntry:
    """One dated, capability-negotiated host catalog entry (opaque by name).

    ``app_ui_supported`` decides the UI-support branch: True keeps the in-host
    app-UI path; False routes to the standard-tool fallback (bounded evidence +
    console deep-link as *additional* help only). ``workspace_proof_required``
    gates whether high-risk profiles may ever bind for this host. ``required_role``
    names the host-admin authority the installer must hold. ``plan_constraints``
    and ``capabilities`` are opaque capability facts, never brand behaviour.
    """

    host_key: str
    display_name: str
    dated_at: str
    capabilities: tuple[str, ...]
    plan_constraints: dict[str, Any]
    required_role: str
    app_ui_supported: bool
    catalog_refresh: dict[str, Any]
    workspace_proof_required: bool

    def as_record(self) -> dict[str, Any]:
        return {
            "host_key": self.host_key,
            # display_name is surfaced for humans but never gates/orders anything.
            "display_name": self.display_name,
            "dated_at": self.dated_at,
            "capabilities": list(self.capabilities),
            "plan_constraints": dict(self.plan_constraints),
            "required_role": self.required_role,
            "app_ui_supported": self.app_ui_supported,
            "catalog_refresh": dict(self.catalog_refresh),
            "workspace_proof_required": self.workspace_proof_required,
        }


# The catalog is a MAPPING keyed by host_key -- intentionally unordered so no
# host can be "first". Dated entries; capability-negotiated. Two entries that
# differ only by name (see the tests) resolve to identical treatment.
_HOST_CATALOG: dict[str, HostCapabilityEntry] = {
    "host_app_ui_v1": HostCapabilityEntry(
        host_key="host_app_ui_v1",
        display_name="Compatible MCP Apps host (in-app UI)",
        dated_at="2026-07-22",
        capabilities=("mcp_apps_ui", "tool_catalog_scan", "workspace_evidence"),
        plan_constraints={"min_plan": "team", "custom_apps": True},
        required_role="host_workspace_admin",
        app_ui_supported=True,
        catalog_refresh={"mode": "rescan_on_version_change", "caches_tools": True},
        workspace_proof_required=True,
    ),
    "host_standard_tools_v1": HostCapabilityEntry(
        host_key="host_standard_tools_v1",
        display_name="Compatible MCP host (standard tools only)",
        dated_at="2026-07-22",
        capabilities=("mcp_tools", "tool_catalog_scan"),
        plan_constraints={"min_plan": "any"},
        required_role="host_workspace_admin",
        app_ui_supported=False,
        catalog_refresh={"mode": "republish_on_version_change", "caches_tools": False},
        workspace_proof_required=False,
    ),
}


def host_catalog() -> dict[str, dict[str, Any]]:
    """Return the maintained host capability catalog as opaque records.

    A MAPPING (never an ordered list): no host is preferred (E36-NFR03). Callers
    that need a display order MUST sort by capability/plan/role, never by name.
    """
    return {key: entry.as_record() for key, entry in _HOST_CATALOG.items()}


def _entry(host_key: str) -> HostCapabilityEntry:
    entry = _HOST_CATALOG.get(host_key)
    if entry is None:
        raise HostPreflightUnavailable("host entry unavailable")
    return entry


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


# ---------------------------------------------------------------------------
# UI-support decision -- app-UI vs standard-tool fallback (AC4).
# ---------------------------------------------------------------------------


def resolve_ui_support(entry: HostCapabilityEntry) -> dict[str, Any]:
    """Decide the UI-support branch for a host entry (capability-driven).

    When ``app_ui_supported`` is True the plan keeps the in-host app-UI path. When
    False the plan selects the standard-tool fallback with MANDATORY bounded
    evidence; a console deep-link is offered only as ADDITIONAL help and is never
    the whole answer (AC4). The decision reads the host's capability flag only --
    never its name.
    """
    if entry.app_ui_supported:
        return {
            "mode": "app_ui",
            "bounded_evidence_required": True,
            "console_deep_link": None,
            "explanation": "L'hote prend en charge l'interface applicative MCP.",
        }
    return {
        "mode": "standard_tool_fallback",
        # Bounded evidence is MANDATORY on the fallback path -- never deep-link-only.
        "bounded_evidence_required": True,
        "console_deep_link": {
            "kind": "console",
            "purpose": "aide_supplementaire",
            "additional_only": True,
        },
        "explanation": (
            "L'hote ne prend pas en charge l'interface applicative : bascule sur "
            "les outils standard avec preuve bornee obligatoire (lien console en aide)."
        ),
    }


# ---------------------------------------------------------------------------
# preflight_host -- record the dated preflight for a selected host (AC1).
# ---------------------------------------------------------------------------


def preflight_host(
    conn,
    *,
    host_key: str,
    task_id: str,
    org_id: str,
    project_id: str | None,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    expires_in_hours: int = 72,
) -> dict[str, Any]:
    """Record a DATED, capability-negotiated preflight for the selected host.

    From the maintained catalog entry it records: dated capabilities, plan
    constraints, required host-admin role, the UI-support decision (app-UI vs
    standard-tool fallback), catalog-refresh behaviour and whether server-verifiable
    workspace proof is required (AC1). The host is opaque: no name-based ordering or
    gating (E36-NFR03). Routes the write through ``execute_operation`` (atomic audit
    + outbox, E36-NFR05). Returns the preflight record (no secret, no Toorow data).
    """
    if not isinstance(host_key, str) or not host_key.strip():
        raise HostPreflightValidationError("host_key is required")
    if not isinstance(org_id, str) or not org_id.strip():
        raise HostPreflightValidationError("org_id is required")
    if not isinstance(task_id, str) or not task_id.strip():
        raise HostPreflightValidationError("task_id is required")
    if not isinstance(expires_in_hours, int) or not 1 <= expires_in_hours <= 168:
        raise HostPreflightValidationError("expires_in_hours must be between 1 and 168")
    entry = _entry(host_key.strip())

    # Bind to the host_connection task: its return condition is the resume signal.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id,t.journey_id,j.org_id,t.state,t.step_key,t.return_condition "
            "FROM app.setup_tasks t JOIN app.setup_journeys j ON j.id=t.journey_id "
            "WHERE t.id=%s FOR UPDATE OF t",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HostPreflightUnavailable("setup task unavailable")
    if row[2] != org_id:
        raise HostPreflightValidationError("task organization does not match the preflight")
    if row[4] != _HOST_TASK_STEP_KEY:
        raise HostPreflightValidationError("task is not the host connection step")
    if row[3] not in {"waiting", "blocked", "ready"}:
        raise HostPreflightConflict("setup task cannot start a preflight")

    preflight_id = f"hostpf_{ULID()}"
    ui_support = resolve_ui_support(entry)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    dated_at = entry.dated_at

    spec = OperationSpec(
        command_type="host.preflight.prepare",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"host-preflight:{preflight_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "host-preflight-v1", "catalog": "host-preflight-v1", "tool": "rest-v1"},
        request_payload={
            # Opaque host identifier + capability facts only; no brand behaviour.
            "preflight_id": preflight_id,
            "host_key": entry.host_key,
            "capabilities": list(entry.capabilities),
            "plan_constraints": entry.plan_constraints,
            "required_role": entry.required_role,
            "app_ui_supported": entry.app_ui_supported,
            "ui_support_mode": ui_support["mode"],
            "workspace_proof_required": entry.workspace_proof_required,
            "dated_at": dated_at,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"host-preflight:{preflight_id}:prepare",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.host_preflights "
                "(id,host_key,org_id,project_id,task_id,capabilities,plan_constraints,"
                "required_role,ui_supported,ui_support_mode,catalog_refresh,"
                "workspace_proof_required,dated_at,state,expires_at,operation_id) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,"
                "'prepared',%s,%s)",
                (
                    preflight_id,
                    entry.host_key,
                    org_id,
                    project_id,
                    task_id,
                    json.dumps(list(entry.capabilities)),
                    json.dumps(entry.plan_constraints),
                    entry.required_role,
                    entry.app_ui_supported,
                    ui_support["mode"],
                    json.dumps(entry.catalog_refresh),
                    entry.workspace_proof_required,
                    dated_at,
                    expires_at,
                    operation_id,
                ),
            )
        result = {
            "preflight_id": preflight_id,
            "host_key": entry.host_key,
            "state": "prepared",
            "capabilities": list(entry.capabilities),
            "plan_constraints": entry.plan_constraints,
            "required_role": entry.required_role,
            "ui_support": ui_support,
            "catalog_refresh": entry.catalog_refresh,
            "workspace_proof_required": entry.workspace_proof_required,
            "dated_at": dated_at,
            "expires_at": expires_at.isoformat(),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "preflight_id": preflight_id,
                "host_key": entry.host_key,
                "transition": "prepared",
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    return operation.result


# ---------------------------------------------------------------------------
# prepare_host_install_handoff -- minimal purpose-scoped handoff (AC2).
# ---------------------------------------------------------------------------


def prepare_host_install_handoff(
    conn,
    *,
    preflight_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    expires_in_hours: int = 72,
) -> dict[str, Any]:
    """Hand the install to the host administrator via a minimal purpose-scoped handoff.

    Used when the operator lacks host-admin authority. Reuses
    ``setup_responsibilities.prepare_handoff`` (the ``host_connection`` task's
    ``install_mcp_host`` action) which reveals only the bound action, expiry and
    return condition -- NO Toorow data, no report, no token (AC2). Marks the
    preflight ``handed_off``. The handoff names the required host-admin ROLE and the
    return condition (``host_connected``); it never grants data access to Toorow.
    """
    from core.setup_responsibilities import prepare_handoff

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,org_id,task_id,required_role,state,expires_at "
            "FROM app.host_preflights WHERE id=%s FOR UPDATE",
            (preflight_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HostPreflightUnavailable("host preflight unavailable")
    if row[4] in TERMINAL_STATES or row[4] == "stale":
        raise HostPreflightConflict("host preflight cannot be handed off")

    task_id, required_role = row[2], row[3]

    # The minimal purpose-scoped handoff carrier. prepare_handoff only ever reveals
    # the bound safe_scope action + return condition + expiry; it carries NO Toorow
    # data by construction (SAFE_SCOPE_KEYS + return_condition only).
    handoff = prepare_handoff(
        conn,
        task_id=task_id,
        actor=actor,
        expires_in_hours=expires_in_hours,
        idempotency_key=f"{idempotency_key}:handoff",
        host_context=host_context,
        trace_id=trace_id,
    )

    _transition(
        conn,
        preflight_id=preflight_id,
        target_state="handed_off",
        actor=actor,
        idempotency_key=f"{idempotency_key}:handoff-state",
        host_context=host_context,
        trace_id=trace_id,
        reason=None,
    )
    return {
        "preflight_id": preflight_id,
        "state": "handed_off",
        # The administrator action named to the operator (no data, no secret).
        "admin_action": {
            "action": "install_mcp_host",
            "required_role": required_role,
            "return_condition": {"kind": _HOST_RETURN_KIND, "resource_id": task_id},
        },
        "handoff_id": handoff.handoff_id,
        "handoff_state": handoff.state,
        "expires_at": handoff.expires_at,
        "delivery_url": handoff.delivery_url,
        "operation_id": handoff.operation_id,
        "audit_event_id": handoff.audit_event_id,
        "replayed": handoff.replayed,
    }


# ---------------------------------------------------------------------------
# bind_host_connection -- reconcile callback + write the capability context (AC3).
# ---------------------------------------------------------------------------


def bind_host_connection(
    conn,
    *,
    preflight_id: str,
    endpoint_binding: str,
    enabled_profiles: list[str],
    workspace_evidence_hash: str | None,
    host: str | None,
    workspace_id: str | None,
    workspace_type: str | None,
    client_id: str | None,
    policy_version: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """On a verified install/authorization, reconcile state and BIND the connection.

    Writes the ``app.mcp_capability_contexts`` row (Story 36.11 / migration 067):
    endpoint profile binding, organization, host/workspace policy and the bound
    catalog version (AC3). The catalog version comes from
    ``mcp_profiles.catalog_version`` for the connection class the enabled profiles
    imply (``admin`` if any high-risk profile is requested, else ``read``).

    HIGH-RISK PROFILES BIND ONLY WITH VERIFIABLE PROOF: a high-risk profile is
    written into ``enabled_profiles`` ONLY when the host entry requires proof, a
    64-hex ``workspace_evidence_hash`` is supplied, AND that hash is bound here.
    Otherwise we fail closed to ``insights`` only -- and ``visible_profiles`` (36.11)
    keeps every high-risk profile hidden regardless of host self-report.

    Reconciles the ``host_connection`` task from SERVER evidence (never a client
    callback) and drives the preflight to ``bound``. Routes through
    ``execute_operation`` (atomic audit + outbox). No token/secret is ever stored.
    """
    from core import mcp_profiles

    if not isinstance(endpoint_binding, str) or not endpoint_binding.strip():
        raise HostPreflightValidationError("endpoint_binding is required")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise HostPreflightValidationError("policy_version is required")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,host_key,org_id,project_id,task_id,workspace_proof_required,state "
            "FROM app.host_preflights WHERE id=%s FOR UPDATE",
            (preflight_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HostPreflightUnavailable("host preflight unavailable")
    (_pid, host_key, org_id, project_id, task_id, proof_required, state) = row
    if state in TERMINAL_STATES:
        if state == "bound":
            raise HostPreflightConflict("host preflight already bound")
        raise HostPreflightConflict("host preflight cannot be bound")
    if state == "stale":
        # A stale preflight must be re-approved (re-run preflight_host) first (AC5).
        raise HostPreflightConflict("host preflight is stale and must be re-approved")

    # Determine which profiles may actually bind. High-risk profiles require BOTH
    # the host entry to require proof AND a valid 64-hex workspace evidence hash.
    requested = [p for p in (enabled_profiles or []) if isinstance(p, str)]
    proof_ok = bool(proof_required) and _is_hex64(workspace_evidence_hash)
    bound_profiles: list[str] = [_DEFAULT_PROFILE]
    for profile in requested:
        if profile == _DEFAULT_PROFILE:
            continue
        if profile in _HIGH_RISK_PROFILES and proof_ok:
            bound_profiles.append(profile)
        # else: fail closed -- high-risk profile dropped without verifiable proof.
    # Dedupe while preserving insights-first order.
    seen: set[str] = set()
    bound_profiles = [p for p in bound_profiles if not (p in seen or seen.add(p))]
    binds_high_risk = any(p in _HIGH_RISK_PROFILES for p in bound_profiles)
    connection_class = "admin" if binds_high_risk else "read"
    catalog_version = mcp_profiles.catalog_version(connection_class)
    # Only persist the evidence hash when it is actually honoured (proof_ok).
    evidence_to_bind = workspace_evidence_hash if (binds_high_risk and proof_ok) else None
    context_id = f"mcpctx_{ULID()}"

    spec = OperationSpec(
        command_type="host.preflight.bind",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"host-preflight:{preflight_id}",
            f"capability-context:{context_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": policy_version,
            "catalog": catalog_version,
            "tool": "rest-v1",
        },
        request_payload={
            "preflight_id": preflight_id,
            "context_id": context_id,
            "host_key": host_key,
            "endpoint_binding": endpoint_binding,
            "enabled_profiles": bound_profiles,
            "connection_class": connection_class,
            "policy_version": policy_version,
            "catalog_version": catalog_version,
            "workspace_proof_bound": evidence_to_bind is not None,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"host-preflight:{preflight_id}:bind",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            # 1. Write the Story 36.11 capability context binding (migration 067).
            cur.execute(
                "INSERT INTO app.mcp_capability_contexts "
                "(id,org_id,host,workspace_id,workspace_type,client_id,endpoint_binding,"
                "enabled_profiles,workspace_evidence_hash,policy_version,catalog_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                (
                    context_id,
                    org_id,
                    host,
                    workspace_id,
                    workspace_type,
                    client_id,
                    endpoint_binding,
                    json.dumps(bound_profiles),
                    evidence_to_bind,
                    policy_version,
                    catalog_version,
                ),
            )
            # 2. Link the preflight to the bound context and drive it to 'bound'.
            cur.execute(
                "UPDATE app.host_preflights SET state='bound',"
                "capability_context_id=%s,updated_at=NOW() "
                "WHERE id=%s AND state IN ('prepared','handed_off','installed')",
                (context_id, preflight_id),
            )
            if cur.rowcount != 1:
                raise HostPreflightConflict("host preflight bind race lost")
            # 3. Reconcile the host_connection task from SERVER evidence (not callback).
            _reconcile_host_task(operation_conn, task_id=task_id)
        result = {
            "preflight_id": preflight_id,
            "capability_context_id": context_id,
            "state": "bound",
            "endpoint_binding": endpoint_binding,
            "org_id": org_id,
            "enabled_profiles": bound_profiles,
            "policy_version": policy_version,
            "catalog_version": catalog_version,
            "connection_class": connection_class,
            "workspace_proof_bound": evidence_to_bind is not None,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": state}),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "preflight_id": preflight_id,
                "capability_context_id": context_id,
                "transition": "bound",
                "host_key": host_key,
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    return operation.result


def _reconcile_host_task(conn, *, task_id: str) -> str:
    """Reconcile the host_connection task from SERVER evidence, not a client callback.

    Mirrors ``source_delegation._reconcile_source_task``: the binding just succeeded
    on the server, so server evidence for ``host_connected`` is true for this
    resource; ``reconcile_task_state`` advances the task deterministically.
    """
    from core.setup_responsibilities import reconcile_task_state

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,return_condition FROM app.setup_tasks WHERE id=%s FOR UPDATE",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HostPreflightUnavailable("setup task unavailable")
    return_condition = row[1]
    resource_id = (return_condition or {}).get("resource_id")
    kind = (return_condition or {}).get("kind")
    evidence = {kind: {resource_id: True}} if kind and resource_id else {}
    new_state = reconcile_task_state(
        current_state=row[0],
        return_condition=return_condition,
        server_evidence=evidence,
    )
    if new_state != row[0]:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.setup_tasks SET state=%s,updated_at=NOW(),"
                "completed_at=CASE WHEN %s='completed' THEN NOW() ELSE completed_at END "
                "WHERE id=%s",
                (new_state, new_state, task_id),
            )
    return new_state


# ---------------------------------------------------------------------------
# invalidate_stale_preflight -- on policy/capability change (AC5).
# ---------------------------------------------------------------------------


def invalidate_stale_preflight(
    conn,
    *,
    preflight_id: str,
    reason: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Invalidate a stale preflight on a policy/capability change (AC5).

    Marks the preflight ``stale`` so it must be re-approved (re-run
    ``preflight_host``) before any bind. High-risk profiles STAY HIDDEN until then:
    the bound capability context (if any) is immutable (migration 067), so a fresh,
    re-approved preflight must mint a NEW context -- a stale preflight can never bind
    a high-risk profile. Preserves any prior valid binding row untouched; it only
    closes the pending/approved preflight so resumption re-approves from scratch.
    """
    return _transition(
        conn,
        preflight_id=preflight_id,
        target_state="stale",
        actor=actor,
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
        reason=reason,
    ).result


# ---------------------------------------------------------------------------
# Shared state-transition helper -- every transition via the operation seam.
# ---------------------------------------------------------------------------


def _transition(
    conn,
    *,
    preflight_id: str,
    target_state: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    reason: str | None,
) -> OperationResult:
    """Drive a preflight state transition through the shared operation seam."""
    if target_state not in PREFLIGHT_STATES:
        raise HostPreflightValidationError("unsupported preflight state")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,org_id,host_key FROM app.host_preflights "
            "WHERE id=%s FOR UPDATE",
            (preflight_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HostPreflightUnavailable("host preflight unavailable")
    current_state, org_id, host_key = row
    if current_state in TERMINAL_STATES and current_state != target_state:
        raise HostPreflightConflict("host preflight already resolved")

    spec = OperationSpec(
        command_type="host.preflight.transition",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"host-preflight:{preflight_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "host-preflight-v1", "catalog": "host-preflight-v1", "tool": "rest-v1"},
        request_payload={
            "preflight_id": preflight_id,
            "target_state": target_state,
            "reason": reason,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"host-preflight:{preflight_id}:{target_state}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.host_preflights SET state=%s,updated_at=NOW() "
                "WHERE id=%s AND state=%s",
                (target_state, preflight_id, current_state),
            )
            if cur.rowcount != 1 and current_state != target_state:
                raise HostPreflightConflict("host preflight transition race lost")
        result = {"preflight_id": preflight_id, "state": target_state}
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": current_state}),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "preflight_id": preflight_id,
                "transition": target_state,
                "host_key": host_key,
            },
        )

    return execute_operation(conn, spec, mutation=mutation)
