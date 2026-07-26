"""Server-authoritative setup responsibilities and purpose-scoped handoffs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ulid import ULID

from core.operations import MutationResult, OperationResult, OperationSpec, execute_operation

ACTOR_TYPES = {"invited_operator", "toorow_admin", "credential_owner", "source_admin", "host_admin"}
RETURN_KINDS = {
    "invitation_accepted",
    "project_access",
    "organization_access",
    "source_authorized",
    "first_report_ready",
    "host_connected",
}
TERMINAL_STATES = {"completed", "failed", "expired", "revoked"}
SAFE_SCOPE_KEYS = {"action", "org_id", "project_id", "source", "host", "report_ref"}
SECRET_KEY = re.compile(
    r"(^|[_-])(bearer|token|secret|password|credential|authorization|api[_-]?key)($|[_-])", re.I
)


class SetupValidationError(ValueError):
    pass


class SetupConflict(RuntimeError):
    pass


class SetupUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class HandoffResult:
    handoff_id: str
    state: str
    expires_at: str
    operation_id: str
    audit_event_id: str | None
    outbox_event_id: str | None
    delivery_url: str | None
    replayed: bool


@dataclass(frozen=True)
class HandoffExchangeResult:
    handoff_id: str
    task_id: str
    purpose: str
    actor_type: str
    safe_scope: dict[str, str]
    return_path: str
    session_value: str
    max_age_seconds: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _pepper() -> str:
    value = os.environ.get("TOOROW_HANDOFF_PEPPER", "")
    if len(value) < 32:
        raise SetupValidationError("TOOROW_HANDOFF_PEPPER must contain at least 32 characters")
    return value


def _keyed_hash(value: str, purpose: str) -> str:
    return hmac.new(_pepper().encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_actor(value: str) -> str:
    if value not in ACTOR_TYPES:
        raise SetupValidationError("unsupported setup actor type")
    return value


def _validate_safe_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(scope, Mapping) or not scope or not set(scope).issubset(SAFE_SCOPE_KEYS):
        raise SetupValidationError("safe_scope contains unsupported keys")
    safe = {}
    for key, value in scope.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise SetupValidationError("safe_scope contains an invalid value")
        safe[key] = value.strip()
    if "action" not in safe:
        raise SetupValidationError("safe_scope action is required")
    return safe


def _validate_return_condition(condition: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(condition, Mapping) or set(condition) != {"kind", "resource_id"}:
        raise SetupValidationError("return_condition must bind kind and resource_id")
    kind, resource_id = condition.get("kind"), condition.get("resource_id")
    if kind not in RETURN_KINDS or not isinstance(resource_id, str) or not resource_id:
        raise SetupValidationError("unsupported return condition")
    return {"kind": kind, "resource_id": resource_id}


def derive_initial_tasks(
    *,
    operator_identity: str,
    toorow_admin_identity: str,
    credential_owner_identity: str | None,
    host_admin_identity: str | None,
    accepted_at: datetime,
    project_id: str | None = None,
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    scope_id = project_id or org_id
    if not scope_id:
        raise SetupValidationError("project_id or org_id is required")
    scope = {"project_id": project_id} if project_id else {"org_id": str(org_id)}
    access_kind = "project_access" if project_id else "organization_access"
    accepted = _aware(accepted_at)
    expiry = accepted + timedelta(days=6)
    common = {"reminder_policy": {"mode": "none"}, "return_path": "/onboarding/responsibilities"}
    return [
        {
            **common,
            "step_key": "invitation_accepted",
            "title": "Invitation acceptée",
            "actor_type": "toorow_admin",
            "owner": toorow_admin_identity,
            "state": "completed",
            "expires_at": None,
            "handoff_method": "none",
            "blocker": None,
            "return_condition": {"kind": "invitation_accepted", "resource_id": scope_id},
            "safe_scope": {**scope, "action": "accept_invitation"},
        },
        {
            **common,
            "step_key": access_kind,
            "title": (
                "Accès au projet confirmé"
                if project_id
                else "Accès à l’organisation confirmé"
            ),
            "actor_type": "invited_operator",
            "owner": operator_identity,
            "state": "completed",
            "expires_at": None,
            "handoff_method": "none",
            "blocker": None,
            "return_condition": {"kind": access_kind, "resource_id": scope_id},
            "safe_scope": {**scope, "action": f"verify_{access_kind}"},
        },
        {
            **common,
            "step_key": "source_authorization",
            "title": "Autoriser la source et exposer le compte",
            "actor_type": "credential_owner",
            "owner": credential_owner_identity or "Propriétaire des identifiants à désigner",
            "state": "waiting",
            "expires_at": expiry,
            "handoff_method": "link",
            "blocker": "Une autre personne doit autoriser la source",
            "return_condition": {"kind": "source_authorized", "resource_id": scope_id},
            "safe_scope": {**scope, "action": "authorize_source"},
        },
        {
            **common,
            "step_key": "first_report",
            "title": "Créer le premier rapport récent",
            "actor_type": "invited_operator",
            "owner": operator_identity,
            "state": "blocked",
            "expires_at": None,
            "handoff_method": "none",
            "blocker": "Autorisation de la source requise",
            "return_condition": {"kind": "first_report_ready", "resource_id": scope_id},
            "safe_scope": {**scope, "action": "create_first_report"},
        },
        {
            **common,
            "step_key": "host_connection",
            "title": "Connecter un hôte MCP Apps",
            "actor_type": "host_admin",
            "owner": host_admin_identity
            or "Administratrice ou administrateur de l’hôte à désigner",
            "state": "waiting",
            "expires_at": expiry,
            "handoff_method": "task",
            "blocker": "Installation dans l’hôte requise",
            "return_condition": {"kind": "host_connected", "resource_id": scope_id},
            "safe_scope": {**scope, "action": "install_mcp_host"},
        },
    ]

def reconcile_task_state(
    *,
    current_state: str,
    return_condition: Mapping[str, Any],
    server_evidence: Mapping[str, Mapping[str, bool]],
    client_payload: Mapping[str, Any] | None = None,
) -> str:
    del client_payload
    condition = _validate_return_condition(return_condition)
    if current_state in TERMINAL_STATES:
        return current_state
    return (
        "completed"
        if server_evidence.get(condition["kind"], {}).get(condition["resource_id"]) is True
        else current_state
    )


def _allowed_actions(state: str, method: str) -> list[str]:
    if state in TERMINAL_STATES:
        return ["prepare_replacement"] if state == "expired" and method != "none" else []
    if state in {"waiting", "blocked"} and method != "none":
        return ["prepare_handoff", "reconcile"]
    return ["continue"] if state == "ready" else ["reconcile"]


def project_safe_task(row: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    observed = _aware(now or datetime.now(timezone.utc))
    expires_at = row.get("expires_at")
    state = str(row.get("state") or "blocked")
    if (
        isinstance(expires_at, datetime)
        and _aware(expires_at) <= observed
        and state not in TERMINAL_STATES
    ):
        state = "expired"
    policy = row.get("reminder_policy") or {"mode": "none"}
    reminder = (
        {"mode": "none", "label": "Aucun rappel automatique"}
        if policy.get("mode") == "none"
        else {"mode": "scheduled", "label": "Rappel planifié", "next_at": policy.get("next_at")}
    )
    task_id = str(row.get("task_id") or row.get("id") or "")
    result = {
        "task_id": task_id,
        "safe_id_suffix": task_id[-6:],
        "step_key": row.get("step_key"),
        "title": row.get("title"),
        "actor_type": row.get("actor_type"),
        "owner": row.get("owner") or row.get("owner_label"),
        "state": state,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        "handoff_method": row.get("handoff_method"),
        "reminder": reminder,
        "return_condition": _validate_return_condition(row.get("return_condition") or {}),
        "return_path": row.get("return_path"),
        "safe_scope": _validate_safe_scope(row.get("safe_scope") or {}),
        "blocker": row.get("blocker"),
        "actions": _allowed_actions(state, str(row.get("handoff_method") or "none")),
    }
    if any(SECRET_KEY.search(key) for key in result):
        raise SetupValidationError("unsafe task projection")
    return result


def get_reconciled_journey(
    conn,
    *,
    project_id: str | None = None,
    org_id: str | None = None,
    server_evidence: Mapping[str, Mapping[str, bool]],
) -> dict[str, Any]:
    if bool(project_id) == bool(org_id):
        raise SetupValidationError("exactly one of project_id or org_id is required")
    with conn.cursor() as cur:
        if project_id:
            cur.execute(
                "SELECT id, org_id, project_id, state, operator_identity, created_at "
                "FROM app.setup_journeys WHERE project_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )
        else:
            cur.execute(
                "SELECT id, org_id, project_id, state, operator_identity, created_at "
                "FROM app.setup_journeys WHERE org_id = %s AND project_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (org_id,),
            )
        journey = cur.fetchone()
        if journey is None:
            raise SetupUnavailable("setup journey unavailable")
        cur.execute(
            "SELECT id, step_key, title, actor_type, owner_label, state, expires_at, "
            "handoff_method, reminder_policy, return_condition, return_path, safe_scope, blocker "
            "FROM app.setup_tasks WHERE journey_id = %s ORDER BY created_at, id",
            (journey[0],),
        )
        rows = cur.fetchall()
    tasks = []
    for row in rows:
        tasks.append(
            project_safe_task(
                {
                    "task_id": row[0],
                    "step_key": row[1],
                    "title": row[2],
                    "actor_type": row[3],
                    "owner": row[4],
                    "state": reconcile_task_state(
                        current_state=row[5],
                        return_condition=row[9],
                        server_evidence=server_evidence,
                    ),
                    "expires_at": row[6],
                    "handoff_method": row[7],
                    "reminder_policy": row[8],
                    "return_condition": row[9],
                    "return_path": row[10],
                    "safe_scope": row[11],
                    "blocker": row[12],
                }
            )
        )
    return {
        "journey_id": journey[0],
        "organization_id": journey[1],
        "project_id": journey[2],
        "state": journey[3],
        "operator": journey[4],
        "progress": {
            "completed": sum(t["state"] == "completed" for t in tasks),
            "total": len(tasks),
        },
        "tasks": tasks,
    }

def bootstrap_journey_from_acceptance(
    conn,
    *,
    invitation_id: str | None,
    org_id: str,
    project_id: str | None,
    operator_identity: str,
    toorow_admin_identity: str,
    accepted_at: datetime,
) -> str:
    journey_id = f"setup_{ULID()}"
    tasks = derive_initial_tasks(
        operator_identity=operator_identity,
        toorow_admin_identity=toorow_admin_identity,
        credential_owner_identity=None,
        host_admin_identity=None,
        project_id=project_id,
        org_id=org_id,
        accepted_at=accepted_at,
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.setup_journeys "
            "(id, org_id, project_id, invitation_id, operator_identity) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (invitation_id) DO NOTHING RETURNING id",
            (journey_id, org_id, project_id, invitation_id, operator_identity),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                "SELECT id FROM app.setup_journeys WHERE invitation_id = %s", (invitation_id,)
            )
            existing = cur.fetchone()
            if existing is None:
                raise SetupConflict("setup journey disappeared")
            return existing[0]
        journey_id = inserted[0]
        for task in tasks:
            cur.execute(
                "INSERT INTO app.setup_tasks "
                "(id, journey_id, step_key, title, actor_type, assigned_identity, "
                "owner_label, state, expires_at, handoff_method, reminder_policy, "
                "return_condition, return_path, safe_scope, blocker, completed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,%s)",
                (
                    f"setuptask_{ULID()}",
                    journey_id,
                    task["step_key"],
                    task["title"],
                    task["actor_type"],
                    task["owner"] if "@" in task["owner"] else None,
                    task["owner"],
                    task["state"],
                    task["expires_at"],
                    task["handoff_method"],
                    json.dumps(task["reminder_policy"]),
                    json.dumps(task["return_condition"]),
                    task["return_path"],
                    json.dumps(task["safe_scope"]),
                    task["blocker"],
                    accepted_at if task["state"] == "completed" else None,
                ),
            )
    return journey_id


def _as_handoff_result(operation: OperationResult, delivery_url: str) -> HandoffResult:
    data = operation.result
    return HandoffResult(
        data["handoff_id"],
        data["state"],
        data["expires_at"],
        operation.operation_id,
        operation.audit_event_id,
        operation.outbox_event_id,
        None if operation.replayed else delivery_url,
        operation.replayed,
    )


def prepare_handoff(
    conn,
    *,
    task_id: str,
    actor: str,
    expires_in_hours: int,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> HandoffResult:
    if not isinstance(expires_in_hours, int) or not 1 <= expires_in_hours <= 168:
        raise SetupValidationError("expires_in_hours must be between 1 and 168")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id,t.journey_id,j.org_id,t.actor_type,t.assigned_identity,t.state,"
            "t.safe_scope,t.return_condition,t.return_path FROM app.setup_tasks t "
            "JOIN app.setup_journeys j ON j.id=t.journey_id WHERE t.id=%s FOR UPDATE OF t",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None or row[5] not in {"waiting", "blocked", "ready"}:
        raise SetupConflict("setup task cannot be handed off")
    actor_type, scope, condition = (
        _validate_actor(row[3]),
        _validate_safe_scope(row[6]),
        _validate_return_condition(row[7]),
    )
    bearer, handoff_id = secrets.token_urlsafe(32), f"handoff_{ULID()}"
    bearer_hash = _keyed_hash(bearer, "handoff-bearer")
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    identity_hash = _keyed_hash(row[4].strip().lower(), "handoff-identity") if row[4] else None
    spec = OperationSpec(
        command_type="setup.handoff.prepare",
        actor=actor,
        effective_org_id=row[2],
        resource_path=(f"organization:{row[2]}", f"setup-task:{task_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "setup-v1", "catalog": "setup-v1", "tool": "rest-v1"},
        request_payload={
            "task_id": task_id,
            "actor_type": actor_type,
            "expires_in_hours": expires_in_hours,
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"setup-task:{task_id}:handoff",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.setup_handoffs "
                "(id,task_id,purpose,actor_type,assigned_identity_hash,bearer_hash,safe_scope,"
                "return_condition,return_path,expires_at,operation_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                (
                    handoff_id,
                    task_id,
                    scope["action"],
                    actor_type,
                    identity_hash,
                    bearer_hash,
                    json.dumps(scope),
                    json.dumps(condition),
                    row[8],
                    expires_at,
                    operation_id,
                ),
            )
            cur.execute(
                "UPDATE app.setup_handoffs SET state='revoked',revoked_at=NOW(),"
                "updated_at=NOW(),superseded_by=%s "
                "WHERE task_id=%s AND id<>%s AND state IN ('created','delivered')",
                (handoff_id, task_id, handoff_id),
            )
        result = {
            "handoff_id": handoff_id,
            "task_id": task_id,
            "state": "created",
            "expires_at": expires_at.isoformat(),
        }
        return MutationResult(
            "succeeded",
            None,
            _canonical_hash(result),
            result,
            {"handoff_id": handoff_id, "task_id": task_id, "delivery_kind": "setup_handoff"},
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    origin = os.environ.get("TOOROW_HANDOFF_ORIGIN", "").rstrip("/")
    if not origin.startswith("https://"):
        raise SetupValidationError("TOOROW_HANDOFF_ORIGIN must be HTTPS")
    return _as_handoff_result(operation, f"{origin}/handoff#handoff={bearer}")


def reassign_task(
    conn,
    *,
    task_id: str,
    actor: str,
    actor_type: str,
    assigned_identity: str | None,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    actor_type = _validate_actor(actor_type)
    assigned = assigned_identity.strip().lower() if assigned_identity else None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id,t.journey_id,j.org_id,t.actor_type,t.assigned_identity,t.state,t.version "
            "FROM app.setup_tasks t JOIN app.setup_journeys j ON j.id=t.journey_id "
            "WHERE t.id=%s FOR UPDATE OF t",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None or row[5] in TERMINAL_STATES:
        raise SetupConflict("setup task cannot be reassigned")
    spec = OperationSpec(
        command_type="setup.task.reassign",
        actor=actor,
        effective_org_id=row[2],
        resource_path=(f"organization:{row[2]}", f"setup-task:{task_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "setup-v1", "catalog": "setup-v1", "tool": "rest-v1"},
        request_payload={
            "task_id": task_id,
            "actor_type": actor_type,
            "assigned": assigned is not None,
            "previous_version": row[6],
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"setup-task:{task_id}:reassign",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.setup_handoffs SET state='revoked',revoked_at=NOW(),updated_at=NOW() "
                "WHERE task_id=%s AND state IN ('created','delivered') RETURNING id",
                (task_id,),
            )
            revoked = [item[0] for item in cur.fetchall()]
            if revoked:
                cur.execute(
                    "UPDATE app.setup_handoff_sessions SET consumed_at=NOW(),"
                    'resolution=\'{"state":"revoked"}\'::jsonb '
                    "WHERE handoff_id=ANY(%s) AND consumed_at IS NULL",
                    (revoked,),
                )
            cur.execute(
                "UPDATE app.setup_tasks SET actor_type=%s,assigned_identity=%s,owner_label=%s,"
                "version=version+1,updated_at=NOW() WHERE id=%s AND version=%s",
                (actor_type, assigned, assigned or actor_type, task_id, row[6]),
            )
            if cur.rowcount != 1:
                raise SetupConflict("setup reassignment race lost")
        result = {
            "task_id": task_id,
            "actor_type": actor_type,
            "assigned": assigned is not None,
            "version": row[6] + 1,
        }
        return MutationResult(
            "succeeded",
            _canonical_hash({"actor_type": row[3], "assigned": row[4] is not None}),
            _canonical_hash(result),
            result,
            {"task_id": task_id, "transition": "reassigned", "actor_type": actor_type},
        )

    return execute_operation(conn, spec, mutation=mutation).result


def exchange_handoff(
    conn, *, bearer: str, presented_identity: str | None = None
) -> HandoffExchangeResult:
    if not isinstance(bearer, str) or len(bearer) < 32:
        raise SetupUnavailable("handoff unavailable")
    bearer_hash, now = _keyed_hash(bearer, "handoff-bearer"), datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,task_id,purpose,actor_type,safe_scope,return_path,expires_at,state,"
            "assigned_identity_hash "
            "FROM app.setup_handoffs WHERE bearer_hash=%s FOR UPDATE",
            (bearer_hash,),
        )
        row = cur.fetchone()
        if row is None or row[7] != "created" or _aware(row[6]) <= now:
            raise SetupUnavailable("handoff unavailable")
        # When the handoff is bound to a known identity, only that identity may
        # consume the one-time bearer -- a leaked/forwarded link cannot be burned
        # by an unintended party. A null binding is an intentionally anonymous
        # handoff (external actor with no toorow account) and stays open.
        assigned_identity_hash = row[8]
        if assigned_identity_hash is not None:
            if not presented_identity:
                raise SetupUnavailable("handoff unavailable")
            presented_hash = _keyed_hash(presented_identity.strip().lower(), "handoff-identity")
            if not secrets.compare_digest(presented_hash, assigned_identity_hash):
                raise SetupUnavailable("handoff unavailable")
        session_value, session_id = secrets.token_urlsafe(32), f"handoffsess_{ULID()}"
        session_hash = _keyed_hash(session_value, "handoff-session")
        cur.execute(
            "INSERT INTO app.setup_handoff_sessions "
            "(id,handoff_id,session_hash,expires_at) VALUES (%s,%s,%s,%s)",
            (session_id, row[0], session_hash, row[6]),
        )
        cur.execute(
            "UPDATE app.setup_handoffs SET state='delivered',updated_at=NOW() "
            "WHERE id=%s AND state='created'",
            (row[0],),
        )
    return HandoffExchangeResult(
        row[0],
        row[1],
        row[2],
        row[3],
        _validate_safe_scope(row[4]),
        row[5],
        session_value,
        max(1, int((_aware(row[6]) - now).total_seconds())),
    )


def revoke_handoff(
    conn,
    *,
    handoff_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.id,h.task_id,h.state,j.org_id FROM app.setup_handoffs h "
            "JOIN app.setup_tasks t ON t.id=h.task_id "
            "JOIN app.setup_journeys j ON j.id=t.journey_id "
            "WHERE h.id=%s FOR UPDATE OF h",
            (handoff_id,),
        )
        row = cur.fetchone()
    if row is None or row[2] in {"completed", "expired", "revoked", "failed"}:
        raise SetupConflict("setup handoff already resolved")
    spec = OperationSpec(
        command_type="setup.handoff.revoke",
        actor=actor,
        effective_org_id=row[3],
        resource_path=(f"organization:{row[3]}", f"setup-handoff:{handoff_id}"),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "setup-v1", "catalog": "setup-v1", "tool": "rest-v1"},
        request_payload={"handoff_id": handoff_id},
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"setup-handoff:{handoff_id}:revoke",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.setup_handoffs SET state='revoked',revoked_at=NOW(),updated_at=NOW() "
                "WHERE id=%s AND state IN ('created','delivered')",
                (handoff_id,),
            )
            if cur.rowcount != 1:
                raise SetupConflict("setup handoff revocation race lost")
            cur.execute(
                "UPDATE app.setup_handoff_sessions SET consumed_at=NOW(),"
                'resolution=\'{"state":"revoked"}\'::jsonb '
                "WHERE handoff_id=%s AND consumed_at IS NULL",
                (handoff_id,),
            )
        result = {"handoff_id": handoff_id, "task_id": row[1], "state": "revoked"}
        return MutationResult(
            "succeeded",
            _canonical_hash({"state": row[2]}),
            _canonical_hash(result),
            result,
            {"handoff_id": handoff_id, "task_id": row[1], "transition": "revoked"},
        )

    return execute_operation(conn, spec, mutation=mutation).result
