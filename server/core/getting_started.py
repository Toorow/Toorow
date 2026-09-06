"""One durable Getting Started journey composed from authoritative owner evidence.

A READ DOES NOT WRITE -- amendment of 2026-08-17 to `overview.md`.

`GET /getting-started` used to create the journey when none existed, UPDATE
`app.setup_tasks` to reconcile every step against readiness, and INSERT an event per
reconciled step; the route then committed. Two consequences, both measured by the
audit of 2026-08-17:

* the capability required to reach that GET is `view`, so the FIRST PERSON TO OPEN
  THE PAGE became `setup_journeys.operator_identity` -- the authoring of a Project's
  setup was attributed to whoever looked first;
* a repository rule ("Une lecture n'écrit pas") was contradicted with no arbitration
  written anywhere.

THE SEAM. `bootstrap_project_journey` was ALREADY called at every point where a
Project or an access to it is created -- `projects_api.py`, `invitations.py`,
`hosted_entry_scope.py`, `self_hosted_instance_claim.py`. Creation owns the
bootstrap; nothing was missing. The GET's copy was a fallback for Projects that
predate those call sites, and it is now an explicit gesture instead:
`POST .../getting-started/journey` (capability `edit`, idempotent).

WHAT THE READ SHOWS MEANWHILE. `read_getting_started` DERIVES each step's state from
the shared readiness projection in memory and never persists it. The displayed state
is therefore always current, whether or not the journal has caught up;
`reconcile_project_journey` writes that same derivation -- states, events and the
journey's own completion -- and runs from every authorized gesture on this surface.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ulid import ULID

from core.project_readiness import READINESS_COMPONENTS, compose_project_readiness

_TASKS = (
    (
        "project_foundation",
        "Confirm Project foundation",
        "project_settings",
        "project_foundation",
        "none",
    ),
    ("source", "Authorize a Source", "source_owner", "source", "link"),
    ("datastream", "Create the first Datastream", "project_operator", "datastream", "none"),
    # `governance` OWNS A READINESS COMPONENT NOW (`project_readiness.py`). While it
    # did not, no evidence could ever complete this step and the journey was capped
    # at 4 of 5 for every Project, forever.
    ("governance", "Review Governance readiness", "governance_owner", "governance", "none"),
    ("first_value", "Publish the first usable value", "project_operator", "first_value", "none"),
)


def _route(org_id: str, project_id: str) -> str:
    return f"/org/{org_id}/project/{project_id}/getting-started"


def bootstrap_project_journey(
    conn,
    *,
    project_id: str,
    actor_identity: str,
    org_id: str | None = None,
    invitation_id: str | None = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id=%s AND status='active'", (project_id,)
        )
        project = cur.fetchone()
        if not project:
            raise ValueError("Project not found")
        if org_id is not None and str(project[0]) != org_id:
            raise ValueError("Project Organization mismatch")
        # ANY journey, not only an ACTIVE one. This asked for `state='active'`, and
        # nothing could ever leave that state, so the predicate was free. Now that
        # a journey can reach `verified`, the same predicate would have made every
        # later bootstrap open a SECOND journey on the same Project -- the partial
        # unique index only forbids two ACTIVE ones. One Project, one journey.
        cur.execute(
            "SELECT id FROM app.setup_journeys WHERE project_id=%s ORDER BY created_at LIMIT 1",
            (project_id,),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        journey_id = f"setup_{ULID()}"
        cur.execute(
            "INSERT INTO app.setup_journeys "
            "(id,org_id,project_id,invitation_id,operator_identity,state) "
            "VALUES (%s,%s,%s,%s,%s,'active')",
            (journey_id, project[0], project_id, invitation_id, actor_identity),
        )
        for step_key, title, actor_type, readiness_key, handoff_method in _TASKS:
            task_id = f"setuptask_{ULID()}"
            owner = _owner_reference(step_key)
            resume_ref = _route(str(project[0]), project_id)
            cur.execute(
                "INSERT INTO app.setup_tasks "
                "(id,journey_id,step_key,title,actor_type,assigned_identity,owner_label,"
                "state,handoff_method,reminder_policy,return_condition,return_path,"
                "safe_scope,authoritative_owner_ref,readiness_ref,resume_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'blocked',%s,"
                "'{\"mode\":\"none\"}'::jsonb,%s::jsonb,%s,%s::jsonb,%s::jsonb,"
                "%s::jsonb,%s)",
                (
                    task_id,
                    journey_id,
                    step_key,
                    title,
                    "invited_operator"
                    if actor_type in {"project_settings", "project_operator"}
                    else "source_admin",
                    actor_identity
                    if actor_type in {"project_settings", "project_operator"}
                    else None,
                    actor_type,
                    handoff_method,
                    json.dumps({"kind": "project_access", "resource_id": project_id}),
                    resume_ref,
                    json.dumps({"project_id": project_id, "action": step_key}),
                    json.dumps(owner),
                    json.dumps({"component": readiness_key} if readiness_key else {}),
                    resume_ref,
                ),
            )
            cur.execute(
                "INSERT INTO app.setup_task_events "
                "(id,journey_id,task_id,event_type,actor_identity,reason,safe_refs) "
                "VALUES (%s,%s,%s,'created',%s,'journey_bootstrap',%s::jsonb)",
                (
                    f"setupevt_{ULID()}",
                    journey_id,
                    task_id,
                    actor_identity,
                    json.dumps({"step_key": step_key}),
                ),
            )
    return journey_id


def _owner_reference(step_key: str) -> dict[str, Any]:
    if step_key == "project_foundation":
        return {
            "surface": "global",
            "workspace": None,
            "section": None,
            "global_surface": "project-settings",
            "global_section": "general",
            "object_type": None,
            "object_id": None,
            "tab": None,
            "action": None,
            "version_id": None,
            "evidence_id": None,
        }
    if step_key == "source":
        workspace, section, obj, action = "data", "sources", None, None
    elif step_key == "datastream":
        # The collection owns `create`; there is no object to name yet.
        workspace, section, obj, action = "data", "datastreams", None, "create"
    elif step_key == "governance":
        workspace, section, obj, action = "governance", "controls-quality", None, None
    else:
        workspace, section, obj, action = "data", "datastreams", None, "create"
    return {
        "surface": "project",
        "workspace": workspace,
        "section": section,
        "global_surface": None,
        "global_section": None,
        "object_type": "datastream" if obj else None,
        "object_id": obj,
        "tab": None,
        "action": action,
        "version_id": None,
        "evidence_id": None,
    }


# Which readiness component owns each step. A step with no component of its own
# is NOT completed by borrowing another one: the Governance review used to read
# `project_foundation`, so it reported "done" as soon as the Project had an
# active configuration version, whatever the state of Governance. It then owned
# NOTHING for a while, which capped the journey at 4 of 5 -- and it now owns the
# `governance` component of the shared projection, which reads Governance's own
# evidence and nobody else's.
_STEP_READINESS = {
    "project_foundation": "project_foundation",
    "source": "source",
    "datastream": "datastream",
    "governance": "governance",
    "first_value": "first_value",
    "source_authorization": "source",
    "first_report": "first_value",
}


def _state_for(step_key: str, readiness: dict[str, Any]) -> str | None:
    """The state readiness DICTATES for this step, or None when it cannot judge it.

    Three persisted step keys carry no readiness component: `invitation_accepted`,
    `project_access` and `host_connection`. This returned the literal `"unknown"`
    for them, and the caller wrote that answer straight into
    `app.setup_tasks.state` -- whose CHECK constraint allows seven values, and
    not that one. So every GET of Getting Started on a project holding one of
    those steps raised CheckViolation, was swallowed by the route's blanket
    handler and answered 503 "Getting Started is unavailable".

    Measured on production 2026-08-04, project `proj_01KYJ0NP...`: five tasks,
    three of them unjudgeable, and the screen has never once rendered.

    Readiness having nothing to say about a step is NOT a state. The persisted
    state stands, and the reconciliation skips it.
    """
    key = _STEP_READINESS.get(step_key)
    if key is None:
        return None
    return "completed" if readiness[key]["state"] == "ready" else "blocked"


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _read_project_identity(conn, project_id: str) -> dict[str, Any]:
    """The Project's NAME, and its Organization's.

    Getting Started rendered `scopeLabel={projectId}` and its eyebrow read
    "Project coordination · proj_01K...". `GlobalScopeLayout` documents that exact
    string as the reason the "Current scope" panel was removed, and the console's
    vocabulary rule is the user's, never the database's. The screen cannot resolve
    a name it is not given, so the envelope carries it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.name, p.org_id, o.name FROM app.projects p "
            "LEFT JOIN app.organizations o ON o.id = p.org_id "
            "WHERE p.id=%s AND p.status='active'",
            (project_id,),
        )
        row = cur.fetchone()
    if not row:
        raise ValueError("Project not found")
    return {
        "id": project_id,
        "name": str(row[0]),
        "organization": (
            {"id": str(row[1]), "name": str(row[2])} if row[1] and row[2] else None
        ),
    }


def read_getting_started(conn, *, project_id: str) -> dict[str, Any]:
    """Project the journey. WRITES NOTHING -- see the module docstring.

    Every step's state is DERIVED here from the shared readiness projection and
    kept in memory. `reconcile_project_journey` is what persists the same
    derivation; until it runs, the screen is still correct because it reads this.
    """
    project = _read_project_identity(conn, project_id)
    readiness = compose_project_readiness(project_id, conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,state,created_at,completed_at,org_id FROM app.setup_journeys "
            "WHERE project_id=%s ORDER BY created_at LIMIT 1",
            (project_id,),
        )
        journey = cur.fetchone()
        rows: list = []
        events: list = []
        if journey:
            cur.execute(
                "SELECT id,step_key,title,state,actor_type,assigned_identity,owner_label,"
                "authoritative_owner_ref,readiness_ref,resume_ref,return_condition,"
                "blocker,handoff_method FROM app.setup_tasks WHERE journey_id=%s "
                "ORDER BY created_at,id",
                (journey[0],),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT id,task_id,event_type,actor_identity,occurred_at,safe_refs "
                "FROM app.setup_task_events WHERE journey_id=%s "
                "ORDER BY occurred_at DESC,id DESC LIMIT 100",
                (journey[0],),
            )
            events = cur.fetchall()

    journey_org_id = str(journey[4]) if journey else None
    projected = []
    journal_behind = False
    for row in rows:
        judged = _state_for(str(row[1]), readiness)
        # A step readiness cannot judge keeps the state it was persisted with.
        # Deriving one would be asserting an opinion nobody holds.
        state = judged if judged is not None else str(row[3])
        if judged is not None and judged != str(row[3]):
            journal_behind = True
        projected.append(
            {
                "id": str(row[0]),
                "step_key": str(row[1]),
                "title": str(row[2]),
                "state": state,
                "owner": {"actor_type": row[4], "identity": row[5], "label": row[6]},
                "authoritative_owner_ref": row[7] or _owner_reference(str(row[1])),
                "readiness_ref": row[8] or {"version": readiness["version"]},
                "handoff_summary": None,
                "return_condition": row[10],
                "resume_ref": row[9] or _route(str(journey_org_id), project_id),
                "blocker": row[11] if state != "completed" else None,
                "actions": ["prepare_handoff"]
                if row[12] != "none" and state == "blocked"
                else [],
            }
        )

    completed = sum(task["state"] == "completed" for task in projected)
    total = len(projected)
    next_task = next((task["id"] for task in projected if task["state"] != "completed"), None)
    # A JOURNEY THAT CAN END. `all()` over an empty list is True, so `total` gates
    # it: a journey with no task is not a finished one.
    finished = total > 0 and completed == total
    persisted_state = str(journey[1]) if journey else None
    if not journey:
        displayed_state = "not_started"
    elif persisted_state == "abandoned":
        displayed_state = "abandoned"
    else:
        displayed_state = "verified" if finished else "active"
    if journey and persisted_state != displayed_state:
        journal_behind = True

    return {
        "schema_version": "getting-started.v2",
        "project": project,
        "journey": {
            "id": str(journey[0]) if journey else None,
            "state": displayed_state,
            "started_at": _iso(journey[2]) if journey else None,
            "completed_at": _iso(journey[3]) if journey else None,
            "progress": {
                "completed": completed,
                "total": total,
                "percent": round((completed / total) * 100) if total else 0,
            },
            "next_task_id": next_task,
        },
        # WHAT THE READER MAY ASK FOR, AND WHY. A Project created before the
        # bootstrap moved to creation has no journey at all; the empty state must
        # name the gesture that fills it rather than describe a deployment.
        # `journal_behind` is not an error and is not shown as one -- the states
        # above are already derived and true; it only says the persisted trail has
        # not caught up, and any authorized gesture on this surface catches it up.
        "materialization": {
            "state": "materialized" if journey else "pending",
            "action": None if journey else "start_journey",
            "journal_behind": journal_behind,
            "explanation": (
                "This Project's setup journey has not been created yet."
                if not journey
                else "The journey is recorded."
            ),
        },
        "tasks": projected,
        "history": [
            {
                "event_id": str(row[0]),
                "task_id": row[1],
                "event_type": str(row[2]),
                "actor": str(row[3]),
                "occurred_at": _iso(row[4]),
                "safe_refs": row[5],
            }
            for row in events
        ],
        "readiness": {
            "version": readiness["version"],
            "components": list(READINESS_COMPONENTS),
            **{key: readiness[key]["state"] for key in READINESS_COMPONENTS},
        },
    }


def reconcile_project_journey(conn, *, project_id: str, actor: str) -> dict[str, Any]:
    """THE write. Creates the journey if it is missing, then journals readiness.

    Idempotent by construction: every statement below is conditioned on a real
    difference, so calling it twice writes once. It is what the explicit
    materialization route runs, and what every authorized gesture on this surface
    runs before acting -- so the journal follows the evidence without any read
    ever writing.
    """
    journey_id = bootstrap_project_journey(conn, project_id=project_id, actor_identity=actor)
    readiness = compose_project_readiness(project_id, conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id,step_key,state FROM app.setup_tasks WHERE journey_id=%s "
            "ORDER BY created_at,id",
            (journey_id,),
        )
        rows = cur.fetchall()
        states: list[str] = []
        for task_id, step_key, persisted in rows:
            judged = _state_for(str(step_key), readiness)
            state = judged if judged is not None else str(persisted)
            states.append(state)
            if judged is None or judged == str(persisted):
                continue
            cur.execute(
                "UPDATE app.setup_tasks SET state=%s,completed_at=CASE "
                "WHEN %s='completed' THEN NOW() ELSE NULL END,updated_at=NOW(),"
                "version=version+1 WHERE id=%s",
                (state, state, task_id),
            )
            cur.execute(
                "INSERT INTO app.setup_task_events "
                "(id,journey_id,task_id,event_type,actor_identity,reason,safe_refs) "
                "VALUES (%s,%s,%s,%s,%s,'readiness_reconciled',%s::jsonb)",
                (
                    f"setupevt_{ULID()}",
                    journey_id,
                    task_id,
                    "completed" if state == "completed" else "reopened",
                    actor,
                    json.dumps({"readiness_version": readiness["version"]}),
                ),
            )
        _write_journey_completion(
            cur,
            journey_id=journey_id,
            actor=actor,
            readiness_version=str(readiness["version"]),
            finished=bool(states) and all(state == "completed" for state in states),
        )
    return read_getting_started(conn, project_id=project_id)


def _write_journey_completion(
    cur,
    *,
    journey_id: str,
    actor: str,
    readiness_version: str,
    finished: bool,
) -> None:
    """The terminal transition the journey never had.

    `grep "UPDATE app.setup_journeys" server` returned NOTHING before this: a
    journey was created `active` and stayed `active` whatever its steps did, and
    `completed_at` -- a column migration 132 added for exactly this -- was never
    written by anyone.

    `verified` is the schema's own word for the terminal state (the CHECK of
    migration 065 allows `active`, `verified`, `abandoned`; there is no
    `completed`), and it is reversible: evidence that stops holding reopens the
    journey rather than leaving a finished badge over a broken Project. An
    `abandoned` journey is left alone -- that state is a human decision, not a
    derivation.
    """
    if finished:
        cur.execute(
            "UPDATE app.setup_journeys SET state='verified',"
            "completed_at=COALESCE(completed_at, NOW()),verified_at=COALESCE(verified_at, NOW()),"
            "updated_at=NOW() WHERE id=%s AND state='active'",
            (journey_id,),
        )
        event = "completed"
    else:
        cur.execute(
            "UPDATE app.setup_journeys SET state='active',completed_at=NULL,verified_at=NULL,"
            "updated_at=NOW() WHERE id=%s AND state='verified'",
            (journey_id,),
        )
        event = "reopened"
    if not cur.rowcount:
        return
    cur.execute(
        "INSERT INTO app.setup_task_events "
        "(id,journey_id,task_id,event_type,actor_identity,reason,safe_refs) "
        "VALUES (%s,%s,NULL,%s,%s,'journey_readiness_reconciled',%s::jsonb)",
        (
            f"setupevt_{ULID()}",
            journey_id,
            event,
            actor,
            json.dumps({"readiness_version": readiness_version}),
        ),
    )
