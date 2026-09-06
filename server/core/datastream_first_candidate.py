"""Take an ALREADY-ARMED Datastream from armed to pulled, and then to Current.

WHY THIS FILE EXISTS (AI-142, measured 2026-08-02). Production holds one fully
armed Datastream -- live credential, current plan version, current mapping
version, a ``datastream_schedule_state`` row on that plan -- and no surface can
make it run. The three doors were measured shut, each for its own reason:

  * ``POST /api/datastreams/{id}/run`` refuses a ``versioned`` Datastream, and
    ``versioned`` is ``current_plan is not None`` (``core/datastreams.py:84``).
    It refuses this Datastream BECAUSE it is armed.
  * ``POST /api/connections/{id}/pull`` enqueues without a ``datastream_id``, so
    the topology guard resolves a module from the credential provider and
    returns ``multi_tool_credential_needs_a_datastream``
    (``core/account_topology.py:677``).
  * ``lifecycle_state`` is written by exactly ONE function --
    ``datastream_activation.publish_activate_mutation`` (:483, :541) -- and that
    function requires a Ready CANDIDATE execution. There is none, and
    ``app.datastream_setup_drafts`` is empty: this Datastream was not born in the
    wizard, so there is no draft to resume.

WHAT WAS ACTUALLY MISSING -- one link, not a surface. The publication half of the
path is already open and already draft-free:

  * ``read_candidate_review`` (``datastream_activation.py:1182``) LEFT JOINs the
    setup materialization (:1191-1193) and, when there is none, falls back to the
    Datastream's own ``schedule_mode`` (:1203-1207). Every field
    ``build_candidate_review`` demands -- ``plan_version_id``,
    ``mapping_version_id``, ``content_hash``, ``row_count``, ``projection_hash``,
    ``output_plan``, ``schedule`` -- is read from ``app.datastream_executions``
    and ``app.datastreams``. NOT ONE of them comes from a draft.
  * its consumer, ``POST .../executions/{execution_id}/publish-activate``
    (``datastream_preconfiguration_api.py:1066`` -> :1010), is keyed on
    (project, datastream, execution) and reads no draft either.

What nothing does is CREATE that candidate for a Datastream the wizard did not
make. ``materialize_draft_mutation`` (:876-891) is the only caller of
``enqueue_activation_work(kind="candidate_materialization", ...)``, and the
recovery path (``operations_mcp._dispatch_recovery``) creates the execution row
and stops: the job that would run the adapter is never enqueued, so the execution
stays ``created`` forever and ``build_candidate_review`` refuses it at :448
("Only a Ready candidate can be reviewed") -- correctly, because nothing pulled.

So this module opens ONE door, through the seams that already exist:
``create_execution`` against the CURRENT pinned versions, then
``enqueue_activation_work``, both inside a single
``operations.execute_operation`` (atomic state + audit + outbox, idempotent
replay). It writes NO ``lifecycle_state``, NO published pointer and NO execution
state: the worker and ``complete_candidate_from_adapter`` own those, exactly as
they do for a wizard-born candidate. Activation itself is NOT reimplemented here
-- ``publish_activate_mutation`` stays the only writer of ``lifecycle_state``;
this module only carries the review to it.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from core.audit import declare_action
from core.execution_states import ACTIVE_STATES as _LOCKING_STATES

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE = declare_action(
    "datastream.candidate.publish_activate"
)
ACTION_DATASTREAM_FIRST_CANDIDATE_DISPATCH = declare_action("datastream.first_candidate.dispatch")


logger = logging.getLogger(__name__)

#: The three activation modes the worker and its drivers accept
#: (``inbound/datastream_activation_worker.py:27``). Mirrored rather than
#: restated loosely: a mode this module accepts and the worker refuses would
#: surface as a job that fails after it was reported dispatched.
MODES: tuple[str, ...] = ("connector_pull", "external_bq", "managed_feed")

#: One durable command name per door. The publish command REUSES the constant the
#: REST seam already audits under (``entry_confirmations`` :27) so both doors group
#: under one command in ``app.operations`` instead of inventing a parallel history.
FIRST_CANDIDATE_COMMAND = ACTION_DATASTREAM_FIRST_CANDIDATE_DISPATCH
PUBLISH_ACTIVATE_COMMAND = ACTION_DATASTREAM_CANDIDATE_PUBLISH_ACTIVATE

#: ``_LOCKING_STATES`` -- the execution states that hold the single-active-
#: execution lock -- is IMPORTED from ``core.execution_states`` at the top of this
#: file, never re-listed. This module used to carry its own copy of the five
#: names, which is how one new state could silently mean two different things in
#: two modules. ``create_execution`` remains the authority that ENFORCES the
#: lock; this only reads it for the arming report.

#: The precedence and its floor moved to ``core.pull_window``: restating them
#: here WAS a second truth, and "imported in spirit" is not imported. The ceiling
#: stays, because it is this door's own rule -- a first candidate samples the
#: history, it does not backfill it.
_WINDOW_CEILING_DAYS = 365


class FirstCandidateRefused(ValueError):
    """Raised when the armed state cannot honestly produce a candidate."""

    code = "first_candidate_refused"


# ---------------------------------------------------------------------------
# Pure helpers -- no I/O, so every invariant below is offline-testable.
# ---------------------------------------------------------------------------


def resolve_mode(source_kind: str | None, module_name: str | None) -> tuple[str | None, str]:
    """Return ``(mode, how_we_know)`` for one Datastream, or ``(None, reason)``.

    ``app.datastreams.source_kind`` is NULLABLE (migration 030 added it to an
    existing table), so a Datastream created before the wizard -- which is exactly
    the case this module exists for -- can carry a ``module_name`` and no
    ``source_kind``. Deriving ``connector_pull`` from a non-null ``module_name`` is
    licensed by the table's own CHECK (``ck_datastreams_source_kind``:
    ``source_kind = 'connector_pull' AND module_name IS NOT NULL``), but only in
    that one direction, so the derivation is REPORTED under ``how_we_know`` rather
    than made silently: a caller must be able to see that the row never declared
    its kind. Anything else fails closed -- guessing a mode picks a driver, and a
    wrong driver would pull from the wrong place.
    """
    kind = (source_kind or "").strip()
    if kind in MODES:
        return kind, "declared"
    if not kind and (module_name or "").strip():
        return "connector_pull", "derived_from_module_name"
    return None, "undeclared"


def effective_window_days(date_window_days: Any, refetch_days: Any) -> int:
    """Return the window ONE run fetches, with the dispatcher's precedence.

    The order (``date_window_days`` wins, ``refetch_days`` is the legacy alias,
    3 is the floor) was RE-TYPED here, and a different precedence would make the
    first candidate cover a different window than every night after it with
    nobody able to see why. It is now the dispatcher's own resolver
    (``core.pull_window``), so the two cannot drift; only the ceiling stays
    local, because it is this door's rule and not the schedule's -- a first
    candidate is a sample of the history, not the backfill.
    """
    from core.pull_window import resolve_window_days  # noqa: PLC0415

    length = resolve_window_days(
        {"date_window_days": date_window_days, "refetch_days": refetch_days}
    )
    return min(length.days, _WINDOW_CEILING_DAYS)


def bounded_interval(window_days: int, *, today: date) -> dict[str, str]:
    """Return the inclusive ``{from, to}`` window for the first pull.

    ``to`` is YESTERDAY, never today: a day still in progress yields a partial
    row count that a later run would silently replace, and ``build_candidate_review``
    would then have frozen a ``row_count`` that no longer describes the data. The
    shape is the one ``operations_mcp._bounded_interval`` already produces, so the
    worker normalizes ONE interval vocabulary rather than two.
    """
    days = max(1, min(int(window_days), _WINDOW_CEILING_DAYS))
    last = today - timedelta(days=1)
    first = last - timedelta(days=days - 1)
    return {"from": first.isoformat(), "to": last.isoformat()}


def evaluate_arming(facts: dict[str, Any]) -> list[dict[str, str]]:
    """Return every reason this Datastream cannot produce a candidate, in order.

    A LIST, not a first failure: a person who has to discover the second blocker
    by re-running the first repair is the "failure discovered at the click" this
    repository names in CLAUDE.md. Each entry is ``{code, detail}`` and carries no
    row content -- codes and column names only, so the report stays safe to show.
    """
    blockers: list[dict[str, str]] = []
    if not facts.get("current_plan_version_id"):
        blockers.append(
            {
                "code": "no_plan_version",
                "detail": "app.datastreams.current_plan_version_id is NULL: nothing to pin.",
            }
        )
    if not facts.get("current_mapping_version_id"):
        blockers.append(
            {
                "code": "no_mapping_version",
                "detail": "app.datastreams.current_mapping_version_id is NULL: nothing to map.",
            }
        )
    elif facts.get("mapping_executable") is not True:
        blockers.append(
            {
                "code": "mapping_not_executable",
                "detail": "the current mapping version is not executable; repair the mapping.",
            }
        )
    if facts.get("projection_executable") is False:
        codes = ",".join(facts.get("projection_issue_codes") or []) or "unknown"
        blockers.append(
            {
                "code": "projection_not_executable",
                "detail": f"the mapping does not compile to an executable projection: {codes}.",
            }
        )
    mode = facts.get("mode")
    if mode not in MODES:
        blockers.append(
            {
                "code": "undeclared_source_kind",
                "detail": (
                    "app.datastreams.source_kind is NULL and no module_name lets it be "
                    "derived; declare the source kind before dispatching."
                ),
            }
        )
    elif mode == "connector_pull" and not facts.get("module_name"):
        blockers.append(
            {
                "code": "no_connector_binding",
                "detail": "source_kind is connector_pull but module_name is NULL.",
            }
        )
    elif mode == "connector_pull" and not facts.get("connection_ref_id"):
        blockers.append(
            {
                "code": "no_connection_binding",
                "detail": "source_kind is connector_pull but connection_ref_id is NULL.",
            }
        )
    if facts.get("active_execution_id"):
        blockers.append(
            {
                "code": "execution_in_flight",
                "detail": (
                    "a non-terminal execution already holds the single-execution lock; "
                    "let it finish or reconcile it first."
                ),
            }
        )
    return blockers


def next_step(facts: dict[str, Any]) -> str:
    """Name the ONE next action, so a caller never has to infer it.

    Deliberately a sentence about what to do, not a status word: "draft" told
    nobody that the missing piece was a candidate execution, which is how this
    Datastream sat armed and idle.
    """
    if facts.get("blockers"):
        return f"repair: {facts['blockers'][0]['code']}"
    if facts.get("ready_candidate_execution_id"):
        return "publish_activate_datastream_candidate with that execution_id"
    if facts.get("pending_candidate_execution_id"):
        return "wait: a candidate is materializing; its adapter has not returned yet"
    return "start_datastream_first_candidate"


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------


def read_arming(conn, *, project_id: str, datastream_id: str) -> dict[str, Any] | None:
    """Return the exact armed state of one Datastream, or None when unknown.

    Reports what IS, never what it wishes were true: every field below is a column
    read, and the derived ones (``mode``, ``window_days``, ``blockers``) are pure
    functions of those columns so a reader can recompute them.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.org_id, d.name, d.lifecycle_state, d.enabled, d.schedule_mode,
                   d.source_kind, d.module_name, d.connection_ref_id, d.report_profile_id,
                   d.current_plan_version_id, d.current_mapping_version_id,
                   d.current_published_execution_id, d.date_window_days, d.refetch_days,
                   m.executable,
                   (SELECT e.id FROM app.datastream_executions e
                     WHERE e.datastream_id = d.id AND e.project_id = d.project_id
                       AND e.state = ANY(%s)
                     ORDER BY e.created_at ASC LIMIT 1),
                   (SELECT e.id FROM app.datastream_executions e
                     WHERE e.datastream_id = d.id AND e.project_id = d.project_id
                       AND e.state = 'ready'
                     ORDER BY e.created_at DESC LIMIT 1),
                   (SELECT count(*) FROM app.datastream_schedule_state s
                     WHERE s.datastream_id = d.id AND s.project_id = d.project_id
                       AND s.plan_version_id = d.current_plan_version_id)
              FROM app.datastreams d
              LEFT JOIN app.datastream_mapping_versions m
                     ON m.id = d.current_mapping_version_id AND m.project_id = d.project_id
             WHERE d.id = %s AND d.project_id = %s
            """,
            (list(_LOCKING_STATES), datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    mode, mode_source = resolve_mode(row[5], row[6])
    active_execution_id = row[15]
    ready_execution_id = row[16]
    facts: dict[str, Any] = {
        "project_id": project_id,
        "datastream_id": datastream_id,
        "org_id": row[0],
        "name": row[1],
        "lifecycle_state": row[2],
        "enabled": bool(row[3]),
        "schedule_mode": row[4],
        "source_kind": row[5],
        "module_name": row[6],
        "connection_ref_id": row[7],
        "report_profile_id": row[8],
        "current_plan_version_id": row[9],
        "current_mapping_version_id": row[10],
        "current_published_execution_id": row[11],
        "mapping_executable": (row[14] is True),
        "mode": mode,
        "mode_source": mode_source,
        "window_days": effective_window_days(row[12], row[13]),
        "has_schedule_state": bool(row[17]),
        "active_execution_id": active_execution_id,
        "ready_candidate_execution_id": ready_execution_id,
        # Pending == holds the lock but is not Ready: the adapter has not returned.
        "pending_candidate_execution_id": (
            active_execution_id if active_execution_id != ready_execution_id else None
        ),
    }
    projection = _compiled_projection(conn, facts)
    facts["projection_executable"] = None if projection is None else bool(projection["executable"])
    facts["projection_issue_codes"] = (
        [] if projection is None else sorted({str(i.get("code")) for i in projection["issues"]})
    )
    facts["blockers"] = evaluate_arming(facts)
    facts["next_step"] = next_step(facts)
    return facts


def _compiled_projection(conn, facts: dict[str, Any]) -> dict[str, Any] | None:
    """Compile the projection from the CURRENT mapping version, or None.

    Compiled here rather than stubbed, because this plan becomes the execution's
    ``projection_plan_ref``, and ``read_candidate_review`` turns that same value
    into the review's ``output_plan`` and ``projection_hash``
    (``datastream_activation.py:1228-1229``). A stub -- which is what
    ``_dispatch_recovery`` writes -- would make ``publish_activate_mutation``
    record an Output whose ``grain_evidence`` is empty while the real mapping has
    a grain: evidence that is not false, but not true either.
    """
    mapping_version_id = facts.get("current_mapping_version_id")
    if not mapping_version_id:
        return None
    from core.datastream_field_mapping import get_mapping_version  # noqa: PLC0415
    from core.datastream_projection import compile_projection  # noqa: PLC0415

    try:
        mapping = get_mapping_version(
            facts["datastream_id"], facts["project_id"], str(mapping_version_id), conn
        )
        return compile_projection(mapping)
    except Exception as exc:  # noqa: BLE001 -- an uncompilable mapping is a blocker.
        logger.warning(
            "first_candidate: projection compile failed ds=%s mapping=%s: %s",
            facts["datastream_id"],
            mapping_version_id,
            exc,
        )
        return {"executable": False, "issues": [{"code": "projection_compile_failed"}]}


# ---------------------------------------------------------------------------
# Write 1: dispatch the first candidate (this is the step that PULLS).
# ---------------------------------------------------------------------------


def first_candidate_mutation(
    conn,
    *,
    operation_id: str,
    facts: dict[str, Any],
    projection: dict[str, Any],
    interval: dict[str, str],
    actor: str,
):
    """Create ONE candidate execution and enqueue its materialization job.

    Runs inside ``operations.execute_operation`` so the execution row, the job row,
    the audit event and the outbox event commit together or not at all -- the job
    must never address an execution that was rolled back. Mirrors
    ``materialize_draft_mutation`` (:827-891) exactly, minus the parts that only a
    draft has: no Datastream is created, no version is saved, no draft is
    transitioned. Writes NOTHING on ``app.datastreams``.
    """
    from core.datastream_publication import (  # noqa: PLC0415
        ConcurrentExecutionActive,
        PublicationError,
        create_execution,
    )
    from core.operations import MutationResult, _canonical_hash  # noqa: PLC0415
    from core.queue import enqueue_activation_work  # noqa: PLC0415
    from core.run_origins import FIRST_CANDIDATE, stamp_origin  # noqa: PLC0415

    project_id = facts["project_id"]
    datastream_id = facts["datastream_id"]
    # Story 63.7: the first candidate of a Datastream says so on itself. Without
    # it the Workbench shows a treatment in flight the day a flux is set up and
    # cannot account for it.
    projection = stamp_origin(projection, FIRST_CANDIDATE)
    try:
        candidate = create_execution(
            datastream_id,
            project_id,
            facts["current_plan_version_id"],
            facts["current_mapping_version_id"],
            projection,
            actor,
            f"{operation_id}:candidate",
            conn,
        )
    except ConcurrentExecutionActive as exc:
        logger.warning("first_candidate: lock held ds=%s: %s", datastream_id, exc)
        return MutationResult(
            outcome="failed",
            before_hash=None,
            after_hash=None,
            result={"reason": "execution_in_flight"},
            outbox_payload={"event": "datastream.first_candidate_refused",
                            "datastream_id": datastream_id},
        )
    except PublicationError as exc:
        logger.warning("first_candidate: candidate rejected ds=%s: %s", datastream_id, exc)
        return MutationResult(
            outcome="failed",
            before_hash=None,
            after_hash=None,
            result={"reason": "candidate_rejected"},
            outbox_payload={"event": "datastream.first_candidate_refused",
                            "datastream_id": datastream_id},
        )

    queued = enqueue_activation_work(
        kind="candidate_materialization",
        project_id=project_id,
        datastream_id=datastream_id,
        execution_id=candidate["id"],
        correlation_id=candidate["id"],
        # `mode` is what selects the driver; `interval` is what bounds the pull.
        # Both travel in the PAYLOAD, not in the projection plan: the plan is
        # schema-validated with `additionalProperties: false`
        # (core/schemas/datastream-projection.schema.json:7), so an extra key
        # there would be a plan no re-validation could accept. Story 63.7's
        # `origin` is on the plan and NOT here for exactly the same reason read
        # the other way round: it was DECLARED in that schema, so the plan stays
        # re-validatable, and it belongs to the execution rather than to the job.
        payload={"mode": facts["mode"], "channel": None, "interval": interval},
        requested_by=actor,
        conn=conn,
    )
    result = {
        "datastream_id": datastream_id,
        "candidate_execution_id": candidate["id"],
        "candidate_state": candidate.get("state"),
        "candidate_job_id": queued["job_id"],
        "job_replayed": bool(queued.get("replayed")),
        "mode": facts["mode"],
        "interval": interval,
        "plan_version_id": facts["current_plan_version_id"],
        "mapping_version_id": facts["current_mapping_version_id"],
        # Said explicitly because it is the whole discipline of this module: the
        # dispatch does not activate, and a caller must not read "succeeded" as
        # "the Datastream is now live".
        "lifecycle_state_unchanged": facts["lifecycle_state"],
    }
    return MutationResult(
        outcome="succeeded",
        before_hash=None,
        after_hash=_canonical_hash(result),
        result=result,
        outbox_payload={"event": "datastream.first_candidate_dispatched", **result},
    )


def dispatch_first_candidate(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    actor: str,
    today: date,
    idempotency_key: str,
):
    """Route exactly ONE durable operation that creates and enqueues the candidate.

    Refuses BEFORE the operation exists when the armed state cannot honestly
    produce a candidate: a durable operation recorded for work that was never
    attempted is a false line in the audit.
    """
    from core.operations import OperationSpec, execute_operation  # noqa: PLC0415

    facts = read_arming(conn, project_id=project_id, datastream_id=datastream_id)
    if facts is None:
        raise FirstCandidateRefused("unknown_datastream")
    if facts["blockers"]:
        raise FirstCandidateRefused(facts["blockers"][0]["code"])
    projection = _compiled_projection(conn, facts)
    if projection is None or not projection.get("executable"):
        raise FirstCandidateRefused("projection_not_executable")
    interval = bounded_interval(facts["window_days"], today=today)

    with conn.transaction():
        return facts, interval, execute_operation(
            conn,
            OperationSpec(
                command_type=FIRST_CANDIDATE_COMMAND,
                actor=actor,
                effective_org_id=facts["org_id"],
                resource_path=("projects", project_id, "datastreams", datastream_id),
                idempotency_key=idempotency_key,
                host_context={},
                versions={
                    "plan": facts["current_plan_version_id"],
                    "mapping": facts["current_mapping_version_id"],
                    "tool": "start_datastream_first_candidate",
                },
                request_payload={"mode": facts["mode"], "interval": interval},
                provider_references={},
                confirmation_mode="human",
                confirmation_reference=datastream_id,
                trace_id=None,
            ),
            mutation=lambda active_conn, operation_id: first_candidate_mutation(
                active_conn,
                operation_id=operation_id,
                facts=facts,
                projection=projection,
                interval=interval,
                actor=actor,
            ),
        )


# ---------------------------------------------------------------------------
# Write 2: carry an EXISTING Ready candidate to the existing activation writer.
# ---------------------------------------------------------------------------


def publish_activate_candidate(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    actor: str,
    idempotency_key: str,
):
    """Publish and activate ONE named candidate. Never picks a candidate itself.

    ``execution_id`` is required and is never resolved from "the latest": the
    review this hands to ``publish_activate_mutation`` freezes a ``content_hash``
    and a ``row_count``, and that function re-checks both under ``FOR UPDATE``
    (:501-504). Choosing the execution on the caller's behalf would mean a person
    approves one set of numbers and a different candidate is published.

    This module composes NOTHING itself: ``read_candidate_review`` builds the
    review and ``publish_activate_mutation`` performs every write. That is the
    point -- ``lifecycle_state`` keeps exactly one writer.
    """
    from core.datastream_activation import (  # noqa: PLC0415
        publish_activate_mutation,
        read_candidate_review,
    )
    from core.operations import OperationSpec, execute_operation  # noqa: PLC0415

    if not str(execution_id).startswith("dse_"):
        raise FirstCandidateRefused("invalid_execution_reference")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id=%s AND project_id=%s",
            (datastream_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise FirstCandidateRefused("unknown_datastream")
    org_id = row[0]
    review = read_candidate_review(
        conn, project_id=project_id, datastream_id=datastream_id, execution_id=execution_id
    )
    with conn.transaction():
        return review, execute_operation(
            conn,
            OperationSpec(
                command_type=PUBLISH_ACTIVATE_COMMAND,
                actor=actor,
                effective_org_id=org_id,
                resource_path=("projects", project_id, "datastreams", execution_id),
                idempotency_key=idempotency_key,
                host_context={},
                versions={
                    "plan": review["plan_version_id"],
                    "mapping": review["mapping_version_id"],
                    "tool": "publish_activate_datastream_candidate",
                },
                request_payload={
                    "execution_id": execution_id,
                    "review_hash": review["review_hash"],
                },
                provider_references={},
                confirmation_mode="human",
                confirmation_reference=review["review_hash"],
                trace_id=None,
            ),
            mutation=lambda active_conn, operation_id: publish_activate_mutation(
                active_conn, review=review, actor=actor, operation_id=operation_id
            ),
        )
