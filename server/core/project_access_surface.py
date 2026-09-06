"""Authoritative Project Access read and change model (Story 46.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ulid import ULID

from core.entry_confirmations import (
    PROJECT_ACCESS_COMMAND,
    consume_entry_confirmation,
    issue_entry_confirmation,
)
from core.operations import MutationResult, OperationSpec, _canonical_hash, execute_operation

_CAPABILITIES = {None, "view", "edit", "manage"}
_ROLE_CAP = {"owner": "manage", "admin": "manage", "member": "edit", "viewer": "view"}
_RANK = {None: 0, "view": 1, "edit": 2, "manage": 3}


class ProjectAccessValidationError(ValueError):
    pass


class ProjectAccessConflict(RuntimeError):
    pass


class ProjectAccessUnavailable(RuntimeError):
    pass


def _effective(role: str, grant: str | None) -> tuple[str | None, str]:
    if role == "owner":
        return "manage", "owner_floor"
    if grant not in _CAPABILITIES or grant is None:
        return None, "grant_required"
    rank = min(_RANK[grant], _RANK[_ROLE_CAP.get(role)])
    value = next((name for name, item in _RANK.items() if item == rank), None)
    return value, "explicit_grant"


def read_project_access(project_id: str, conn, *, actor: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id,p.name,p.org_id,o.name FROM app.projects p "
            "JOIN app.organizations o ON o.id=p.org_id "
            "WHERE p.id=%s AND p.status='active' AND o.status='active'",
            (project_id,),
        )
        project = cur.fetchone()
        if not project:
            raise ProjectAccessUnavailable("Project unavailable")
        org_id = str(project[2])
        cur.execute(
            "SELECT m.identity,m.role,m.version,g.capability FROM app.org_members m "
            "LEFT JOIN app.resource_grants g ON g.org_id=m.org_id "
            "AND g.identity=m.identity AND g.scope_type='project' AND g.scope_id=%s "
            "WHERE m.org_id=%s AND m.status='active' ORDER BY m.identity",
            (project_id, org_id),
        )
        members = cur.fetchall()
        cur.execute(
            "SELECT id,identity,state,expires_at,resume_ref "
            "FROM app.project_access_handoffs WHERE project_id=%s "
            "AND state IN ('pending','delivered') ORDER BY created_at DESC LIMIT 100",
            (project_id,),
        )
        handoffs = cur.fetchall()
    people = []
    caller_capability = None
    for identity, role, version, grant in members:
        capability, source = _effective(str(role), str(grant) if grant else None)
        person = {
            "identity": str(identity),
            "organization_role": str(role),
            "membership_version": int(version),
            "explicit_grant": str(grant) if grant else None,
            "effective_capability": capability,
            "grant_source": source,
        }
        people.append(person)
        if identity == actor:
            caller_capability = capability
    if caller_capability is None:
        raise ProjectAccessUnavailable("Project unavailable")
    return {
        "schema_version": "project-access.v1",
        "project": {
            "id": str(project[0]),
            "name": str(project[1]),
            "organization": {"id": org_id, "name": str(project[3])},
        },
        "caller_capability": caller_capability,
        "people": people,
        "handoffs": [
            {
                "id": str(row[0]),
                "identity": row[1],
                "state": str(row[2]),
                "expires_at": row[3].isoformat() if isinstance(row[3], datetime) else row[3],
                "resume_ref": row[4],
            }
            for row in handoffs
        ],
    }


def prepare_grant_change(
    conn,
    *,
    project_id: str,
    identity: str,
    after_capability: str | None,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if after_capability not in _CAPABILITIES:
        raise ProjectAccessValidationError("after_capability must be view, edit, manage or null")
    if not identity or not idempotency_key:
        raise ProjectAccessValidationError("identity and Idempotency-Key are required")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.org_id,m.role,m.status,m.version,g.capability FROM app.projects p "
            "JOIN app.org_members m ON m.org_id=p.org_id AND m.identity=%s "
            "LEFT JOIN app.resource_grants g ON g.org_id=p.org_id "
            "AND g.identity=m.identity AND g.scope_type='project' AND g.scope_id=p.id "
            "WHERE p.id=%s AND p.status='active' FOR SHARE OF p,m",
            (identity, project_id),
        )
        row = cur.fetchone()
        if not row or row[2] != "active":
            raise ProjectAccessValidationError("active Organization membership required")
        if row[1] == "owner" and after_capability is None:
            raise ProjectAccessValidationError("the owner floor cannot be revoked")
        change_id = f"pgrantchg_{ULID()}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        cur.execute(
            "INSERT INTO app.project_grant_changes "
            "(id,project_id,org_id,identity,membership_version,before_capability,"
            "after_capability,actor_identity,expires_at,idempotency_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (project_id,actor_identity,idempotency_key) "
            "DO UPDATE SET id=app.project_grant_changes.id RETURNING id,state,expires_at",
            (
                change_id,
                project_id,
                row[0],
                identity,
                row[3],
                row[4],
                after_capability,
                actor,
                expires_at,
                idempotency_key,
            ),
        )
        stored = cur.fetchone()
    return {
        "change_id": str(stored[0]),
        "state": str(stored[1]),
        "expires_at": stored[2].isoformat(),
        "identity": identity,
        "before": row[4],
        "after": after_capability,
        "membership_version": row[3],
    }


def _change_payload(
    conn, change_id: str, *, project_id: str
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """Load one grant change, PINNED to the project whose URL carried it.

    The route is ``/api/projects/{project_id}/access/grant-changes/{change_id}/...``
    and the caller's authority is resolved on the URL's ``project_id``. Loading
    the change by ``change_id`` alone -- which is what this did until the audit of
    2026-08-17 (report 12, P1-1) -- meant the authority proved on one project was
    spent on a sub-resource belonging to another. The class is generic: every
    ``/{parent}/{sub_id}`` route must pin ``sub.parent_id`` to the URL's parent,
    as ``invitations_api._authorize_invitation_binding`` already does with
    ``org_id IS NOT DISTINCT FROM %s``.

    A mismatch is indistinguishable from a change that does not exist: the same
    ``ProjectAccessUnavailable`` -> 404 "Project not found" envelope, so nobody
    learns from this route that a given change id is real somewhere else.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,project_id,org_id,identity,membership_version,before_capability,"
            "after_capability,actor_identity,state,expires_at,idempotency_key "
            "FROM app.project_grant_changes WHERE id=%s AND project_id=%s FOR UPDATE",
            (change_id, project_id),
        )
        row = cur.fetchone()
    if not row:
        raise ProjectAccessUnavailable("Grant change unavailable")
    payload = {
        "change_id": str(row[0]),
        "project_id": str(row[1]),
        "org_id": str(row[2]),
        "identity": str(row[3]),
        "membership_version": int(row[4]),
        "before": row[5],
        "after": row[6],
    }
    return payload, row


def issue_grant_confirmation(
    conn, *, change_id: str, actor: str, project_id: str
) -> dict[str, Any]:
    payload, row = _change_payload(conn, change_id, project_id=project_id)
    if row[8] != "prepared" or row[9] <= datetime.now(timezone.utc) or row[7] != actor:
        raise ProjectAccessConflict("Grant change is not confirmable")
    issued = issue_entry_confirmation(
        conn,
        actor_person_id=actor,
        command_type=PROJECT_ACCESS_COMMAND,
        request_payload=payload,
        idempotency_key=str(row[10]),
        context_reference=f"project-access:{row[1]}:{change_id}",
    )
    return {
        "confirmation_id": issued.confirmation_id,
        "confirmation_secret": issued.confirmation_secret,
        "expires_at": issued.expires_at.isoformat(),
    }


def confirm_grant_change(
    conn,
    *,
    change_id: str,
    actor: str,
    confirmation_id: str,
    confirmation_secret: str,
    project_id: str,
) -> dict[str, Any]:
    payload, row = _change_payload(conn, change_id, project_id=project_id)
    if row[8] == "confirmed":
        return {"change_id": change_id, "state": "confirmed", "replayed": True}
    if row[8] != "prepared" or row[9] <= datetime.now(timezone.utc) or row[7] != actor:
        raise ProjectAccessConflict("Grant change is not confirmable")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role,status,version FROM app.org_members "
            "WHERE org_id=%s AND identity=%s FOR UPDATE",
            (row[2], row[3]),
        )
        member = cur.fetchone()
    if not member or member[1] != "active" or int(member[2]) != int(row[4]):
        raise ProjectAccessConflict("Organization membership changed")
    consume_entry_confirmation(
        conn,
        confirmation_id=confirmation_id,
        confirmation_secret=confirmation_secret,
        actor_person_id=actor,
        command_type=PROJECT_ACCESS_COMMAND,
        request_payload=payload,
        idempotency_key=str(row[10]),
        context_reference=f"project-access:{row[1]}:{change_id}",
    )
    with conn.cursor() as cur:
        if row[6] is None:
            cur.execute(
                "DELETE FROM app.resource_grants WHERE org_id=%s AND identity=%s "
                "AND scope_type='project' AND scope_id=%s",
                (row[2], row[3], row[1]),
            )
        else:
            cur.execute(
                "INSERT INTO app.resource_grants "
                "(id,org_id,identity,scope_type,scope_id,capability,granted_by) "
                "VALUES (%s,%s,%s,'project',%s,%s,%s) "
                "ON CONFLICT (org_id,identity,scope_type,scope_id) "
                "DO UPDATE SET capability=EXCLUDED.capability,"
                "granted_by=EXCLUDED.granted_by",
                (f"rgrant_{ULID()}", row[2], row[3], row[1], row[6], actor),
            )
        cur.execute(
            "UPDATE app.project_grant_changes SET state='confirmed',confirmed_at=NOW() "
            "WHERE id=%s AND state='prepared'",
            (change_id,),
        )
    return {
        "change_id": change_id,
        "state": "confirmed",
        "identity": row[3],
        "capability": row[6],
        "replayed": False,
    }


def prepare_access_handoff(
    conn,
    *,
    project_id: str,
    identity: str | None,
    actor: str,
    resume_ref: str,
    expires_in_hours: int = 48,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not resume_ref.startswith("/org/") or "#" in resume_ref or "\\" in resume_ref:
        raise ProjectAccessValidationError("resume_ref must be a same-origin canonical route")
    if not 1 <= expires_in_hours <= 168:
        raise ProjectAccessValidationError("expires_in_hours must be between 1 and 168")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ProjectAccessValidationError("Idempotency-Key is required")
    handoff_id = f"paccess_{ULID()}"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id=%s AND status='active'", (project_id,)
        )
        project = cur.fetchone()
        if not project:
            raise ProjectAccessUnavailable("Project unavailable")
    org_id = str(project[0])
    request_payload = {
        "project_id": project_id,
        "identity": identity,
        "resume_ref": resume_ref,
        "expires_in_hours": expires_in_hours,
    }
    spec = OperationSpec(
        command_type="project_access.handoff.prepare",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(f"organization:{org_id}", f"project:{project_id}", "access-handoff"),
        idempotency_key=key,
        host_context={},
        versions={"policy": "project-access-v1", "tool": "rest-v1"},
        request_payload=request_payload,
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"project-access:{project_id}:handoff",
        trace_id=None,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.project_access_handoffs "
                "(id,project_id,org_id,identity,state,resume_ref,expires_at,created_by) "
                "VALUES (%s,%s,%s,%s,'pending',%s,%s,%s)",
                (handoff_id, project_id, org_id, identity, resume_ref, expires_at, actor),
            )
        result = {
            "handoff_id": handoff_id,
            "state": "pending",
            "expires_at": expires_at.isoformat(),
            "resume_ref": resume_ref,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "handoff_id": handoff_id,
                "project_id": project_id,
                "delivery_kind": "project_access_handoff",
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    return {
        **operation.result,
        "operation_id": operation.operation_id,
        "replayed": operation.replayed,
    }
