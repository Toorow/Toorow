"""The project-scoped capability that decides whether anything may LEAVE.

WHAT THIS MODULE OWNS: one column of `app.project_preferences`
(`external_sharing`), its default, its refusal sentence and the audited write
that changes it. Nothing else. It reads no Render, mints no bearer and knows
nothing about the Share ceremony -- `core.render_shares` asks it a yes/no
question and that is the whole coupling.

WHY IT IS A PREFERENCE AND NOT A `project_capabilities` ROW.
`docs/product-architecture/project-settings.md` gives the six capability keys a
compiler, a per-Datastream coverage projection, a dependency graph and an impact
review, because each of them changes what a Datastream produces. External
sharing changes no figure and compiles into no Datastream; a seventh key would
owe a compiler with nothing to compile, and its coverage would be
`Not applicable` on every Datastream forever. It belongs with the Project's
"explicit effective defaults", which is `app.project_preferences`.

WHY THE DEFAULT IS `forbidden`.
`proactive-assertions.md` decision 2 requires the switch and does not fix its
default, so this lot fixes it and writes it into both documents. The reason is
the one `project-settings.md` already gives for every optional capability --
"optional capabilities start Disabled unless the operator confirms a proposal"
-- and it binds harder here, because publishing a link outside the platform is
the one act of this product that cannot be taken back from the person who
received it: revocation stops the NEXT open, never the copy already read.

THE REFUSAL NAMES A GESTURE, NOT A CAUSE. `external_sharing = 'forbidden'` is a
sentence nobody outside this file ever sees. What a person meets is who can turn
it on and where -- CLAUDE.md's rule for every error message, and the only useful
answer when the thing blocking you is a decision somebody else has to take.
"""

from __future__ import annotations

from typing import Any

from core.operations import MutationResult, OperationSpec, execute_operation

#: The two postures. A third value is refused by
#: `chk_project_preferences_external_sharing` (migration 323).
EXTERNAL_SHARING_ALLOWED = "allowed"
EXTERNAL_SHARING_FORBIDDEN = "forbidden"

#: What a Project has before anyone decides -- including a Project with no
#: preferences row at all, which is why this constant exists rather than a
#: `COALESCE` in one query.
EXTERNAL_SHARING_DEFAULT = EXTERNAL_SHARING_FORBIDDEN

#: The command `app.operations` records for a change of posture.
SET_EXTERNAL_SHARING_COMMAND = "project.external_sharing.set"

#: The one sentence that names the gesture. It is a module constant so the API,
#: the domain refusal and the console test all quote the SAME words: a repair
#: instruction that is spelled two ways is two instructions.
ENABLE_GESTURE = (
    "A person holding the Manage role on this project can allow it in "
    "Project settings > General > External sharing."
)


class ExternalSharingValidationError(ValueError):
    """The requested posture is not one of the two."""

    code = "invalid_external_sharing"


class ExternalSharingForbidden(PermissionError):
    """This Project forbids anything leaving the platform."""

    code = "external_sharing_forbidden"

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or (
                "This project does not allow sharing outside the platform. "
                + ENABLE_GESTURE
            )
        )


def read_external_sharing(conn, *, project_id: str, lock: bool = False) -> dict[str, Any]:
    """The posture, and who decided it.

    A Project with no preferences row reads as the platform default with
    `decided_by = None`, and the screen says exactly that. Reporting an absent
    row as a decision somebody took would attribute a refusal to a person who
    never made one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT external_sharing, external_sharing_decided_by,
                   external_sharing_decided_at
              FROM app.project_preferences
             WHERE project_id = %s
            """
            + (" FOR SHARE" if lock else ""),
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {
            "state": EXTERNAL_SHARING_DEFAULT,
            "decided_by": None,
            "decided_at": None,
            "is_platform_default": True,
        }
    return {
        "state": row[0],
        "decided_by": row[1],
        "decided_at": row[2].isoformat() if row[2] else None,
        "is_platform_default": row[1] is None,
    }


def external_sharing_allowed(conn, *, project_id: str, lock: bool = False) -> bool:
    return read_external_sharing(conn, project_id=project_id, lock=lock)["state"] == (
        EXTERNAL_SHARING_ALLOWED
    )


def assert_external_sharing_allowed(conn, *, project_id: str) -> None:
    """Raise before any other work when the Project forbids the exit.

    It is the FIRST statement of `create_share` and of `confirm_share`, ahead of
    the freeze and ahead of the operation row: a refusal that has already frozen
    a payload has done work on behalf of an act the Project forbade.

    THE ROW IS LOCKED `FOR SHARE` UNTIL THE CALLER'S TRANSACTION ENDS (re-review
    of fb073500, residue 2): without it, a `forbidden` committed between this
    read and the bearer-minting UPDATE was not re-gated -- a sub-second window
    in which the exit outran the switch. `FOR SHARE` blocks the concurrent
    `set_external_sharing` UPDATE until the mint commits, so the posture read
    here really is the posture the transition commits against. A project with
    no preferences row has nothing to lock and defaults to `forbidden`, which
    refuses anyway.
    """
    if not external_sharing_allowed(conn, project_id=project_id, lock=True):
        raise ExternalSharingForbidden()


def set_external_sharing(
    conn,
    *,
    org_id: str,
    project_id: str,
    state: str,
    actor: str,
    idempotency_key: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Move the posture, audited, in one transaction.

    Routed through `core.operations.execute_operation` for the same reason the
    Share itself is: the posture and the row that says who moved it commit
    together or not at all. A switch that changed without an audit row would make
    "who allowed this project to publish outside" unanswerable, which is the
    question the switch exists to have an answer to.
    """
    if state not in {EXTERNAL_SHARING_ALLOWED, EXTERNAL_SHARING_FORBIDDEN}:
        raise ExternalSharingValidationError(
            "external sharing is either allowed or forbidden"
        )

    recorded: dict[str, Any] = {}

    def _mutation(mutation_conn, operation_id: str) -> MutationResult:
        with mutation_conn.cursor() as cur:
            # A Project with no preferences row gets an EMPTY one: every other
            # column keeps its unchosen state, so turning sharing on never
            # records a currency or a timezone nobody selected.
            cur.execute(
                "INSERT INTO app.project_preferences (project_id) VALUES (%s) "
                "ON CONFLICT (project_id) DO NOTHING",
                (project_id,),
            )
            cur.execute(
                """
                UPDATE app.project_preferences
                   SET external_sharing = %s,
                       external_sharing_decided_by = %s,
                       external_sharing_decided_at = NOW(),
                       updated_at = NOW()
                 WHERE project_id = %s
             RETURNING external_sharing, external_sharing_decided_by,
                       external_sharing_decided_at
                """,
                (state, actor, project_id),
            )
            row = cur.fetchone()
        if row is None:
            return MutationResult(
                outcome="failed",
                before_hash=None,
                after_hash=None,
                result={"project_id": project_id, "applied": False},
                outbox_payload={"project_id": project_id},
            )
        recorded.update(
            {
                "state": row[0],
                "decided_by": row[1],
                "decided_at": row[2].isoformat() if row[2] else None,
                "is_platform_default": False,
            }
        )
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=None,
            result={"project_id": project_id, "external_sharing": row[0]},
            outbox_payload={"project_id": project_id, "external_sharing": row[0]},
        )

    outcome = execute_operation(
        conn,
        OperationSpec(
            command_type=SET_EXTERNAL_SHARING_COMMAND,
            actor=actor,
            effective_org_id=org_id,
            resource_path=(f"project:{project_id}", "external-sharing"),
            idempotency_key=idempotency_key,
            host_context={},
            versions={},
            request_payload={"external_sharing": state},
            provider_references={},
            # The console shows the consequence and asks before it sends. The
            # two-person ceremony belongs to the SHARE, not to the switch: making
            # the switch itself need a second person would mean a project could
            # never turn sharing on for the first time without one already being
            # available to confirm it.
            confirmation_mode="server",
            confirmation_reference=None,
            trace_id=trace_id,
        ),
        mutation=_mutation,
    )
    if outcome.replayed or not recorded:
        return read_external_sharing(conn, project_id=project_id)
    return recorded


__all__ = [
    "ENABLE_GESTURE",
    "EXTERNAL_SHARING_ALLOWED",
    "EXTERNAL_SHARING_DEFAULT",
    "EXTERNAL_SHARING_FORBIDDEN",
    "SET_EXTERNAL_SHARING_COMMAND",
    "ExternalSharingForbidden",
    "ExternalSharingValidationError",
    "assert_external_sharing_allowed",
    "external_sharing_allowed",
    "read_external_sharing",
    "set_external_sharing",
]
