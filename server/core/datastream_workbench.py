"""Project-scoped, source-agnostic Datastream Workbench read models."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Sequence

from ulid import ULID

from core import business_identity_catalogue as catalogue
from core import execution_states
from core.execution_progress import COLLECTION_PLAN_KIND, PROGRESS_STEPS
from core.run_origins import origin_of

logger = logging.getLogger(__name__)

#: THE `cost` TAB IS CONDITIONAL ON A CAPABILITY, AND ITS ROUTE IS NOT.
#:
#: The amendment « Une capacité activée AJOUTE son onglet » of
#: `datastream-workbench-and-wizard.md` makes `Cost` appear when `tax_fees` is
#: active and disappear otherwise -- "ni onglet, ni panneau, ni colonne". That
#: conditionality is decided by ONE authority, the capability state, and the
#: route serves it: an address that 404'd on a disabled capability would be a
#: second opinion, and the two would disagree the moment somebody enables the
#: capability in another tab. The payload of an inactive capability carries the
#: state and NO measure, and the console refuses the address before it is opened.
#:
#: `placements` JOINS IT ON THE SAME TERMS (story 61.1). `placement_mapping` is
#: the second capability that opens a tab, its route is generated from this tuple
#: like every other, and the payload of an inactive capability carries the state
#: and NO plan, NO line and NO warehouse read. The band and the router both refuse
#: the tab from `CONDITIONAL_OBJECT_TABS`, which is one authority, not two.
#:
#: `mapping` COMES BEFORE `data`, CORRECTED 2026-09-02. Amendment 3 of the review
#: of 2026-08-05 -- « `Mapping` vient avant `Data` » -- fixed the order at
#: `overview`, `mapping`, `data`, [`cost`], [`placements`], `processing`, `runs`,
#: `outputs`; story 58.2 applied it to `datastreamTabs.ts` and to
#: `shell/navigation/data.ts` and to nothing else, so this tuple went on spelling
#: the order that amendment reversed for four weeks. Nothing was red: the console
#: builds its band from its own list, and this tuple is read as a SET (route
#: mounting, `TAB_SCHEMAS`). That is exactly what makes it a defect worth a guard
#: rather than a bug -- a second spelling of one rule, correct nowhere and
#: contradicted nowhere. `scripts/check_amendment_agreement.py` now holds the two
#: spellings together, because neither language can import the other's list.
TABS = (
    "overview", "mapping", "data", "cost", "placements", "processing", "runs", "outputs",
)
TAB_SCHEMAS = {tab: f"datastream_workbench.{tab}.v1" for tab in TABS}


#: How many runs the `Runs` tab returns, newest first.
#:
#: Story 63.1 is what makes this table GROW: one row per Datastream per night,
#: where before a row appeared only when someone published. Unbounded, the tab
#: returned every run since activation -- 365 rows after a year, each now
#: carrying seven more columns. 200 is the bound its sibling already uses for a
#: job list (`admin_api`, pull jobs), and the payload says the list is capped so
#: a reader never mistakes the cap for the whole history.
RUN_LIST_LIMIT = 200


class WorkbenchNotFound(LookupError):
    """The Project-scoped Datastream is unavailable or denied."""


class WorkbenchValidationError(ValueError):
    """The requested Workbench projection is invalid."""


_STAGES = frozenset({"collected", "mapped", "processed", "published"})
_PHASES = frozenset({"pull", "import", "load", "mapping", "processing", "dq", "publication"})
_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {"credential", "credentials", "secret", "access_token", "refresh_token", "authorization"}
)


def _evidence_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _evidence_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _evidence_keys(child)


def append_stage_evidence(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    stage: str,
    actor: str,
    plan_version_id: str,
    mapping_version_id: str,
    evidence: dict[str, Any],
) -> str:
    """Idempotently append one safe, execution-bound physical-stage fact."""
    if stage not in _STAGES or not isinstance(evidence, dict):
        raise WorkbenchValidationError("Execution stage evidence is invalid")
    # `grain_evidence` is CHECKed by `app.safe_preconfiguration_evidence`
    # (migration 134), which requires `jsonb_typeof(value) = 'object'` -- so an
    # array is refused by the database, and so is the column's own DEFAULT
    # `'[]'::jsonb` (migration 138 declares both). This writer defaulted to that
    # same refused `[]`, so EVERY caller that carries no grain -- every candidate
    # materialization of every mode -- died here on a raw CheckViolation, two
    # statements after the work had already been done and committed elsewhere.
    #
    # Two call sites in `datastream_activation` were repaired by wrapping their
    # own value; the writer's default was not, and the writer is what the other
    # callers reach. An object default is the repair at the writer. A caller that
    # supplies a non-object is refused BY NAME here rather than as a 500 from the
    # constraint, because a CheckViolation names the column and not the caller.
    grain_evidence = evidence.get("grain_evidence")
    if grain_evidence is not None and not isinstance(grain_evidence, dict):
        raise WorkbenchValidationError(
            "Execution stage grain evidence must be an object, not a bare list"
        )
    if set(_evidence_keys(evidence)) & _FORBIDDEN_EVIDENCE_KEYS:
        raise WorkbenchValidationError("Secret-bearing stage evidence is forbidden")
    if len(json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")) > 16_384:
        raise WorkbenchValidationError("Execution stage evidence exceeds the safe limit")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT plan_version_id,mapping_version_id
                 FROM app.datastream_executions
                WHERE id=%s AND datastream_id=%s AND project_id=%s""",
            (execution_id, datastream_id, project_id),
        )
        execution = cur.fetchone()
        if execution != (plan_version_id, mapping_version_id):
            raise WorkbenchValidationError("Stage evidence version binding changed")
        cur.execute(
            """SELECT id,evidence FROM app.datastream_execution_stage_evidence
                WHERE execution_id=%s AND stage=%s""",
            (execution_id, stage),
        )
        existing = cur.fetchone()
        normalized = deepcopy(evidence.get("evidence") or {})
        if existing is not None:
            if _json(existing[1], {}) != normalized:
                raise WorkbenchValidationError("Stage evidence already exists with different facts")
            return str(existing[0])
        evidence_id = f"dsse_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_execution_stage_evidence
               (id,org_id,project_id,datastream_id,execution_id,stage,phase_state,
                occurred_at,interval_start,interval_end,plan_version_id,mapping_version_id,
                projection_version_ref,artifact_ref,materialization_ref,schema_hash,
                profile_evidence,coverage_evidence,row_count,grain_evidence,safe_error,
                evidence,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s,%s,%s,%s,%s,%s,
                       %s::jsonb,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)""",
            (
                evidence_id,
                org_id,
                project_id,
                datastream_id,
                execution_id,
                stage,
                str(evidence.get("phase_state") or "succeeded"),
                evidence.get("occurred_at"),
                evidence.get("interval_start"),
                evidence.get("interval_end"),
                plan_version_id,
                mapping_version_id,
                evidence.get("projection_version_ref"),
                evidence.get("artifact_ref"),
                evidence.get("materialization_ref"),
                evidence.get("schema_hash"),
                json.dumps(evidence.get("profile_evidence") or {}, sort_keys=True),
                json.dumps(evidence.get("coverage_evidence") or {}, sort_keys=True),
                evidence.get("row_count"),
                json.dumps(grain_evidence or {}, sort_keys=True),
                json.dumps(evidence.get("safe_error") or {}, sort_keys=True),
                json.dumps(normalized, sort_keys=True),
                actor,
            ),
        )
    return evidence_id


def append_phase_evidence(
    conn,
    *,
    org_id: str,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    phase: str,
    actor: str,
    plan_version_id: str,
    mapping_version_id: str,
    evidence: dict[str, Any],
) -> str:
    """Idempotently append one safe execution phase for the universal Runs timeline."""
    if phase not in _PHASES or not isinstance(evidence, dict):
        raise WorkbenchValidationError("Execution phase evidence is invalid")
    if set(_evidence_keys(evidence)) & _FORBIDDEN_EVIDENCE_KEYS:
        raise WorkbenchValidationError("Secret-bearing phase evidence is forbidden")
    if len(json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")) > 16_384:
        raise WorkbenchValidationError("Execution phase evidence exceeds the safe limit")
    normalized = deepcopy(evidence.get("evidence") or {})
    with conn.cursor() as cur:
        cur.execute(
            """SELECT plan_version_id,mapping_version_id
                 FROM app.datastream_executions
                WHERE id=%s AND datastream_id=%s AND project_id=%s""",
            (execution_id, datastream_id, project_id),
        )
        if cur.fetchone() != (plan_version_id, mapping_version_id):
            raise WorkbenchValidationError("Phase evidence version binding changed")
        cur.execute(
            """SELECT id,evidence FROM app.datastream_execution_phase_evidence
                WHERE execution_id=%s AND phase=%s""",
            (execution_id, phase),
        )
        existing = cur.fetchone()
        if existing is not None:
            if _json(existing[1], {}) != normalized:
                raise WorkbenchValidationError("Phase evidence already exists with different facts")
            return str(existing[0])
        evidence_id = f"dspe_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_execution_phase_evidence
               (id,org_id,project_id,datastream_id,execution_id,phase,phase_state,
                occurred_at,interval_start,interval_end,plan_version_id,mapping_version_id,
                trace_ref,artifact_ref,row_count,quota_evidence,safe_error,evidence,created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s,%s,%s,%s,%s,
                       %s::jsonb,%s::jsonb,%s::jsonb,%s)""",
            (
                evidence_id,
                org_id,
                project_id,
                datastream_id,
                execution_id,
                phase,
                str(evidence.get("phase_state") or "succeeded"),
                evidence.get("occurred_at"),
                evidence.get("interval_start"),
                evidence.get("interval_end"),
                plan_version_id,
                mapping_version_id,
                evidence.get("trace_ref"),
                evidence.get("artifact_ref"),
                evidence.get("row_count"),
                json.dumps(evidence.get("quota_evidence") or {}, sort_keys=True),
                json.dumps(evidence.get("safe_error") or {}, sort_keys=True),
                json.dumps(normalized, sort_keys=True),
                actor,
            ),
        )
    return evidence_id


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return deepcopy(default)
    return deepcopy(value)


def _rows(cur) -> list[dict[str, Any]]:
    columns = [description[0] for description in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _row(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    columns = [description[0] for description in cur.description]
    return dict(zip(columns, row, strict=True))


def _publication_axis(record: dict[str, Any]) -> str:
    if record.get("current_published_execution_id"):
        return "Rollback available" if record.get("last_known_good_execution_id") else "Current"
    if record.get("candidate_execution_id"):
        return "Candidate"
    return "No current publication"


def _configuration_axis(record: dict[str, Any]) -> str:
    if (
        record.get("configuration_blocked")
        or record.get("mapping_executable") is False
        or (record.get("mapping_blocking_count") or 0) > 0
    ):
        return "Blocked"
    if record.get("proposed_plan_version_id") or record.get("proposed_mapping_version_id"):
        return "Change pending"
    if record.get("current_plan_version_id") and record.get("current_mapping_version_id"):
        return "Ready"
    return "Incomplete"


def _operations_evidence(record: dict[str, Any]) -> dict[str, Any]:
    """Name the schedule evidence behind the Operations axis.

    (Every citation in this module was a `file:line` until 2026-08-06, and all
    seven had drifted -- `…wizard.md:87` had become a sentence about adapters,
    `data.md:235` a heading, `README.md:123` a table row about sharing. A ratified
    document is edited daily; a line number in a citation is a reference that will
    be wrong by the time somebody follows it. Names only.)

    `datastream-workbench-and-wizard.md`, the `Operations` line of « State model »,
    fixes the axis as *"derived from schedule, freshness, coverage and latest run
    evidence"*. Only the last of the
    four was read: the axis tested `record["is_stale"]`, a key `_read_base_record`
    never selected and never computed (the only producers of that name live in
    `datastream_activation.py` / `datastream_preconfiguration.py` and mean a
    DIFFERENT thing -- a proposal whose upstream versions drifted). The `Stale`
    branch was therefore unreachable, and a Datastream whose last run succeeded six
    weeks ago and which has not run since read `Healthy` forever.

    No freshness THRESHOLD is invented here -- thresholds are a downstream decision
    (`data.md`, « Evidence supplied to other workspaces »). The schedule already
    states when the next run is due, so
    lateness is read from the Datastream's own declared cadence:

      * `overdue`         -- `next_run_at` is in the past;
      * `missed_runs`     -- the scheduler recorded missed occurrences;
      * `schedule_unarmed`-- lifecycle is Active on a current plan, yet no schedule
        state row exists. Nothing will bring more data. That is not Healthy, and it
        is not provably Stale either (a manual-cadence feed legitimately has none),
        so it resolves to `Unknown` -- invariant 8 of
        `docs/product-architecture/README.md`: *"Unknown, unavailable or
        unverifiable is never healthy"*.
    """
    reasons: list[str] = []
    if record.get("schedule_missed_run_count"):
        reasons.append("missed_runs")
    if record.get("schedule_overdue"):
        reasons.append("overdue")
    return {
        # `app.datastreams.schedule_mode`, the cadence the row declares. `None`
        # only when the column is genuinely NULL -- the screen says "No cadence
        # set" then, and that sentence is finally true when it appears.
        "mode": record.get("schedule_mode"),
        "next_run_at": record.get("schedule_next_run_at"),
        "missed_run_count": record.get("schedule_missed_run_count"),
        "schedule_state_known": bool(record.get("schedule_state_known")),
        "late_reasons": reasons,
    }


def _operations_axis(record: dict[str, Any]) -> str:
    """Health, read from the last run that FINISHED -- never from one in flight.

    Two repairs live here, and they are the same repair.

    THE STATE IS CLASSIFIED BY THE REGISTRY, not by three literal sets. The sets
    listed `published/ready/succeeded` as healthy and knew nothing of
    `collected`, so the day story 63.1 made every nightly retrieval mint a run,
    every scheduled Datastream would have answered `Unknown`.

    THE RUN READ IS THE LAST TERMINAL ONE. `latest_run_state` is now `loading`
    for the whole duration of every nightly pull. Health is a property of what
    finished; a run in flight has not said anything yet, and letting it drive
    this axis would flip a Healthy Datastream to `Unknown` every night for the
    length of its own pull. The in-flight run is reported separately -- it is
    progress, not health.
    """
    state = str(
        record.get("latest_terminal_run_state") or record.get("latest_run_state") or ""
    ).lower()
    if not state:
        return execution_states.AXIS_UNKNOWN
    axis = execution_states.operations_axis(state)
    if axis in (execution_states.AXIS_BLOCKED, execution_states.AXIS_DEGRADED):
        return axis
    if _operations_evidence(record)["late_reasons"]:
        return "Stale"
    if axis == execution_states.AXIS_HEALTHY:
        # A success is only Healthy while the schedule proves data keeps arriving.
        # Active + a current plan + no schedule state = never armed: Unknown.
        if (
            str(record.get("lifecycle_state") or "").lower() == "active"
            and record.get("current_plan_version_id")
            and not record.get("schedule_state_known")
        ):
            return execution_states.AXIS_UNKNOWN
        return execution_states.AXIS_HEALTHY
    return execution_states.AXIS_UNKNOWN


def _primary_action(record: dict[str, Any]) -> dict[str, str]:
    if str(record.get("lifecycle_state") or "draft").lower() == "draft":
        # UN PROCHAIN PAS QUI RENVOIE OU L'ON EST N'EN EST PAS UN. Cette carte
        # vit SUR l'Overview et portait `tab: "overview"` : le bouton ramenait a
        # l'ecran affiche. Constat de Jean, capture a l'appui, 2026-08-12.
        #
        # Ce qu'il reste a faire est connu, et dans cet ordre : publier le
        # mapping qui attend -- sans lui rien en aval ne lit -- puis armer
        # l'horloge. On envoie donc a l'endroit ou le geste se fait.
        if record.get("proposed_mapping_version_id") and not record.get(
            "current_mapping_version_id"
        ):
            return {
                "kind": "publish_mapping",
                "label": "Publish the mapping",
                "reason": "A mapping version is waiting; nothing downstream reads this yet.",
                "tab": "mapping",
            }
        # L'horloge ne s'arme qu'une fois qu'il y a quelque chose a faire
        # tourner : un Datastream sans mapping n'a pas de prochain pas la-bas.
        if record.get("current_mapping_version_id") and not record.get("schedule_state_known"):
            return {
                "kind": "arm_schedule",
                "label": "Arm the schedule",
                "reason": "The clock was never armed, so no run will start on its own.",
                "tab": "runs",
            }
        return {
            "kind": "finish_setup",
            "label": "Finish setup",
            "reason": "The Datastream is still Draft.",
            "tab": "overview",
        }
    if record.get("candidate_execution_id"):
        return {
            "kind": "review_candidate",
            "label": "Review candidate",
            "reason": "A non-live candidate is ready for review.",
            "tab": "outputs",
        }
    if _operations_axis(record) in {"Blocked", "Degraded", "Stale"}:
        return {
            "kind": "repair",
            "label": "Repair",
            "reason": "Operational evidence requires an eligible repair.",
            "tab": "runs",
        }
    return {
        "kind": "prepare_change",
        "label": "Prepare change",
        "reason": "Active evidence is stable; changes require review.",
        "tab": "processing",
    }


def _display_person(record: dict[str, Any]) -> str | None:
    """The display name of the creator, or an honest sentence -- never a ULID."""
    identity = record.get("created_by")
    if not identity:
        return None
    named = record.get("created_by_display_name") or record.get("created_by_email")
    if named:
        return str(named)
    if str(identity) == "anonymous":
        return "Created before sign-in recorded a person"
    return "A member of this organization"


def compose_header(record: dict[str, Any]) -> dict[str, Any]:
    """Compose the shared header without browser inference or a generic score."""
    datastream_id = str(record.get("id") or "")
    project_id = str(record.get("project_id") or "")
    if not datastream_id or not project_id:
        raise WorkbenchValidationError("Datastream and Project identity are required")
    config = _json(record.get("config"), {})
    source_owner = config.get("source_owner") or {}
    return {
        "schema": "datastream_workbench.header.v1",
        "identity": {
            "datastream_id": datastream_id,
            "project_id": project_id,
            "name": record.get("name"),
            "mode": record.get("source_kind") or "connector_pull",
            "data_role": record.get("data_role"),
            # WHO CREATED IT, IN A PERSON'S WORDS -- never the row key. The
            # header rendered "Performance data - person_01KYHQX4RK24Z6QJCYWX6DYVSQ"
            # (mesure 2026-08-12), the same defect as the Credentials screen's
            # "Connected by" column, one screen away and unfixed. `app.persons`
            # carries id and timestamps only, so a ULID names nobody.
            "owner": _display_person(record),
            "module": record.get("module_name"),
            "connector": config.get("connector_name") or record.get("module_name"),
            "source_account_ref": source_owner.get("selected_account_ref"),
            "declared_writer": source_owner.get("declared_writer"),
            "business_domains": deepcopy(record.get("business_domains") or []),
            # AI-113: the delivery channels are part of "identity and bindings",
            # which the ratified Overview contract puts on this tab. Without
            # them the console cannot tell an inbound Datastream from an upload
            # one, so the surface that issues its address had nowhere to appear
            # -- and the panel that issues an address sat mounted nowhere for
            # a week.
            "delivery_channels": sorted(
                {
                    str(channel).strip().lower()
                    for channel in (config.get("channels") or [])
                    if str(channel).strip()
                }
            ),
        },
        "axes": {
            "lifecycle": str(record.get("lifecycle_state") or "draft").title(),
            "configuration": _configuration_axis(record),
            "operations": _operations_axis(record),
            "publication": _publication_axis(record),
        },
        "versions": {
            "active_plan": record.get("current_plan_version_id"),
            "active_plan_number": record.get("active_plan_number"),
            "active_mapping": record.get("current_mapping_version_id"),
            "active_mapping_number": record.get("active_mapping_number"),
            "proposed_plan": record.get("proposed_plan_version_id"),
            "proposed_plan_number": record.get("proposed_plan_number"),
            "proposed_mapping": record.get("proposed_mapping_version_id"),
            "proposed_mapping_number": record.get("proposed_mapping_number"),
            # What governed publication actually consumes. Distinct from
            # `proposed_mapping` on purpose: that one is a version row, this one
            # is the `ready` proposal, and only a proposal can be published.
            "ready_mapping_proposal": record.get("ready_mapping_proposal_id"),
        },
        "runs": {
            "latest": record.get("latest_execution_id"),
            "latest_state": record.get("latest_run_state"),
        },
        # The schedule evidence the Operations axis was derived from, so the axis
        # is inspectable rather than asserted (the `Operations` line of « State
        # model » in `datastream-workbench-and-wizard.md`).
        "operations_evidence": _operations_evidence(record),
        "publications": {
            "candidate": record.get("candidate_execution_id"),
            "current": record.get("current_published_execution_id"),
            "last_known_good": record.get("last_known_good_execution_id"),
        },
        "links": {
            "source": (
                f"/org/{record.get('org_id') or 'org_default'}/project/{project_id}"
                "/data/sources"
            ),
            "project_settings": (
                f"/org/{record.get('org_id') or 'org_default'}/project/{project_id}"
                "/settings/general"
            ),
            "governance": (
                f"/org/{record.get('org_id') or 'org_default'}/project/{project_id}"
                "/governance/master-data"
            ),
        },
        "primary_action": _primary_action(record),
    }


def compose_tab_payload(
    tab: str,
    *,
    datastream_id: str,
    project_id: str,
    evidence: Any,
) -> dict[str, Any]:
    if tab not in TAB_SCHEMAS:
        raise WorkbenchValidationError("Unknown Datastream Workbench tab")
    return {
        "schema": TAB_SCHEMAS[tab],
        "tab": tab,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "evidence": deepcopy(evidence),
    }


def _read_base_record(conn, project_id: str, datastream_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT d.id,d.project_id,d.name,d.source_kind,d.data_role,d.created_by,d.org_id,
                      up.display_name AS created_by_display_name,
                      up.email AS created_by_email,
                      d.module_name,d.config,d.lifecycle_state,d.current_plan_version_id,
                      d.current_mapping_version_id,d.current_published_execution_id,
                      latest.id AS latest_execution_id,latest.state AS latest_run_state,
                      latest_terminal.state AS latest_terminal_run_state,
                      candidate.id AS candidate_execution_id,
                      prior.prior_execution_id AS last_known_good_execution_id,
                      proposed_plan.id AS proposed_plan_version_id,
                      proposed_mapping.id AS proposed_mapping_version_id,
                      -- The `ready` mapping PROPOSAL, which is a different object
                      -- from the proposed mapping VERSION above and could not be
                      -- derived from it. `proposed_mapping` is the newest version
                      -- row that is not current; governed publication consumes a
                      -- proposal (`mapping_proposals`, migration 071), and only
                      -- from state 'ready' -- the only state Story 36.18 may
                      -- publish from.
                      --
                      -- Without this column the Workbench announced "a proposed
                      -- mapping is waiting" and could name nothing to publish, so
                      -- `POST /api/governance/publication-reviews` -- the ONLY
                      -- out-of-band path to `confirm_and_publish` -- had no caller
                      -- and governed publication was unreachable (AI-193).
                      ready_proposal.id AS ready_mapping_proposal_id,
                      -- VERSION NUMBERS
                      ap.version_number AS active_plan_number,
                      am.version_number AS active_mapping_number,
                      proposed_plan.version_number AS proposed_plan_number,
                      proposed_mapping.version_number AS proposed_mapping_number,
                      -- MAPPING HEALTH, the fourth thing this Workbench's own
                      -- function names ("verify freshness, run reliability,
                      -- volume and mapping health") and the only one nothing
                      -- read. `_configuration_axis` already tests
                      -- `configuration_blocked` — a key NOTHING in this repo
                      -- ever wrote, so its `Blocked` branch could not fire and
                      -- a Datastream whose mapping refuses to execute reported
                      -- `Ready`. The Data fleet has joined these two columns
                      -- since it was rebuilt (`data_surface.py`); the Workbench
                      -- simply never asked for them.
                      mv.blocking_count AS mapping_blocking_count,
                      mv.executable AS mapping_executable,
                      -- THE CADENCE ITSELF, and nothing here ever selected it.
                      -- `WorkbenchOverviewPage` reads `schedule.mode` under its
                      -- `Next run` metric and falls back to "No cadence set";
                      -- because this column never left the database, that hint
                      -- read "No cadence set" on EVERY Datastream, including
                      -- the 742 `nightly` rows of the fixture base. A sentence
                      -- that denies a setting the row actually carries is worse
                      -- than a blank: it answers the question wrongly instead
                      -- of declining to answer it.
                      d.schedule_mode AS schedule_mode,
                      sched.next_run_at AS schedule_next_run_at,
                      sched.missed_run_count AS schedule_missed_run_count,
                      sched.overdue AS schedule_overdue,
                      (sched.next_run_at IS NOT NULL
                        OR sched.missed_run_count IS NOT NULL) AS schedule_state_known
                 FROM app.datastreams d
                 JOIN app.project_flux pf ON pf.flux_id=d.id AND pf.project_id=d.project_id
                 LEFT JOIN app.user_profiles up ON up.identity=d.created_by
                 LEFT JOIN app.datastream_mapping_versions mv
                   ON mv.id=d.current_mapping_version_id
                  AND mv.datastream_id=d.id AND mv.project_id=d.project_id
                 LEFT JOIN app.datastream_plan_versions ap
                   ON ap.id=d.current_plan_version_id
                  AND ap.datastream_id=d.id AND ap.project_id=d.project_id
                 LEFT JOIN app.datastream_mapping_versions am
                   ON am.id=d.current_mapping_version_id
                  AND am.datastream_id=d.id AND am.project_id=d.project_id
                 LEFT JOIN LATERAL (
                     SELECT e.id,e.state FROM app.datastream_executions e
                      WHERE e.datastream_id=d.id AND e.project_id=d.project_id
                      ORDER BY e.created_at DESC LIMIT 1
                 ) latest ON TRUE
                 -- Story 63.1: the last run that FINISHED, which is what health
                 -- is read from. `latest` is now `loading` for the length of
                 -- every nightly pull, and health is a property of what ended.
                 LEFT JOIN LATERAL (
                     SELECT e.state FROM app.datastream_executions e
                      WHERE e.datastream_id=d.id AND e.project_id=d.project_id
                        AND e.state = ANY(%(terminal_states)s)
                      ORDER BY e.created_at DESC LIMIT 1
                 ) latest_terminal ON TRUE
                 -- The states are the registry's, passed as a parameter. The
                 -- literal list that stood here also named `outcome_unknown`,
                 -- which `state` can never hold, and did NOT name `collected` --
                 -- so a recurring retrieval in flight was offered as a candidate
                 -- to review for the whole of its run.
                 LEFT JOIN LATERAL (
                     SELECT e.id FROM app.datastream_executions e
                      WHERE e.datastream_id=d.id AND e.project_id=d.project_id
                        AND e.id IS DISTINCT FROM d.current_published_execution_id
                        AND e.state = ANY(%(candidate_states)s)
                        -- A recurring collection is not a candidate anyone is
                        -- asked to review: it publishes nothing by design.
                        AND COALESCE(
                            e.projection_plan_ref->>'kind', ''
                        ) <> %(collection_kind)s
                      ORDER BY e.created_at DESC LIMIT 1
                 ) candidate ON TRUE
                 LEFT JOIN LATERAL (
                     SELECT l.prior_execution_id FROM app.datastream_publication_log l
                      WHERE l.datastream_id=d.id AND l.project_id=d.project_id
                      ORDER BY l.published_at DESC LIMIT 1
                 ) prior ON TRUE
                 LEFT JOIN LATERAL (
                     SELECT p.id, p.version_number FROM app.datastream_plan_versions p
                      WHERE p.datastream_id=d.id AND p.project_id=d.project_id
                        AND p.id IS DISTINCT FROM d.current_plan_version_id
                      ORDER BY p.version_number DESC LIMIT 1
                 ) proposed_plan ON TRUE
                 LEFT JOIN LATERAL (
                     SELECT m.id, m.version_number FROM app.datastream_mapping_versions m
                      WHERE m.datastream_id=d.id AND m.project_id=d.project_id
                        AND m.id IS DISTINCT FROM d.current_mapping_version_id
                      ORDER BY m.version_number DESC LIMIT 1
                 ) proposed_mapping ON TRUE
                 LEFT JOIN LATERAL (
                     SELECT mp.id FROM app.mapping_proposals mp
                      WHERE mp.datastream_id=d.id AND mp.project_id=d.project_id
                        AND mp.state='ready'
                      ORDER BY mp.created_at DESC LIMIT 1
                 ) ready_proposal ON TRUE
                 LEFT JOIN LATERAL (
                     SELECT s.next_run_at,s.missed_run_count,
                            (s.next_run_at IS NOT NULL AND s.next_run_at < now())
                                AS overdue
                       FROM app.datastream_schedule_state s
                      WHERE s.datastream_id=d.id AND s.project_id=d.project_id
                        AND s.plan_version_id=d.current_plan_version_id
                      LIMIT 1
                 ) sched ON TRUE
                WHERE d.id=%(datastream_id)s AND d.project_id=%(project_id)s
                  AND d.archived_at IS NULL""",
            {
                "datastream_id": datastream_id,
                "project_id": project_id,
                # Story 63.1: the classification comes from the registry, so this
                # query cannot fall out of step with the console or the machine.
                "terminal_states": list(execution_states.TERMINAL_STATES),
                "candidate_states": list(execution_states.CANDIDATE_STATES),
                "collection_kind": COLLECTION_PLAN_KIND,
            },
        )
        record = _row(cur)
        if record is None:
            raise WorkbenchNotFound("Datastream Workbench not found")
        # Story 49.2: the Datastream's business domains resolve through the
        # authority, so a domain minted in Master Data still names the datastream
        # it explains instead of vanishing from the header band.
        cur.execute(
            f"""SELECT l.taxonomy_id AS id,bd.name,bd.slug
                 FROM app.mdm_business_links l
                 JOIN {catalogue.DOMAIN_SOURCE} bd
                   ON bd.id=l.taxonomy_id AND bd.org_id=l.org_id
                WHERE l.project_id=%s AND l.target_type='datastream' AND l.target_id=%s
                  AND l.taxonomy_type='business_domain' AND bd.status='active'
                  AND l.retired_at IS NULL
                ORDER BY bd.name,l.taxonomy_id""",
            (project_id, datastream_id),
        )
        record["business_domains"] = _rows(cur)
    return record


#: The SEVEN capabilities the database accepts, and the tab each one OPENS.
#:
#: `tax_fees` opens `Cost` and `placement_mapping` opens `Placements` (amendment
#: « Une capacité activée AJOUTE son onglet »); the other five open NO tab and say
#: so with `None` rather than being left out. Story 58.9: the Overview shows one
#: row per capability with its switch, so the header has to carry the five that
#: add nothing as well as the two that add a tab -- a screen that read only the
#: tab-opening capabilities would show two modules out of seven.
#:
#: Naming `placements` here does not make the tab exist: the console decides what
#: the band renders from `CONDITIONAL_OBJECT_TABS` and `DATASTREAM_TABS`, and
#: story 61.1 is what adds it there together with the panel it opens. What this
#: entry carries is the state, which is what the Overview row needs.
CAPABILITY_TABS: tuple[tuple[str, str | None], ...] = (
    ("country", None),
    ("currency_fx", None),
    ("reporting_timezone", None),
    ("tax_fees", "cost"),
    ("competitors", None),
    ("placement_mapping", "placements"),
    # Story 70.3. `None`, and that is the answer rather than an omission:
    # Analytics Alignment ADDS COLUMNS to an aggregation (three of them,
    # `core/analytics_alignment.ADDED_COLUMNS`) and opens no tab of its own. The
    # Overview still shows its row with its switch, which is exactly why the four
    # -- now five -- capabilities that add no tab are listed here at all.
    ("analytics_alignment", None),
)


def _capability_tabs(conn, project_id: str) -> list[dict[str, Any]]:
    """Which conditional tabs are open, and in which state each capability is.

    IT TRAVELS ON THE HEADER, which every tab of the Workbench already loads, so
    the console learns which tabs exist from the same request that tells it what
    the Datastream is. A second request would leave a window in which the band is
    drawn without the answer -- and a band that draws a conditional tab for one
    frame has shown a tab that must not exist.

    ONE STATEMENT FOR THE SEVEN. `read_capability_states` is the plural reader of
    `project_capability_states`; seven singular calls on a header every tab loads
    is a cost nobody agreed to pay. `availability` travels too, because it is what
    decides whether a switch may exist at all -- migration 131 REFUSES `disabled`
    on an `always_present` capability, so a screen offering that control would be
    offering a write the database throws out.
    """
    from core.project_capability_states import (  # noqa: PLC0415
        capability_is_active,
        read_capability_states,
    )

    states = read_capability_states(
        conn,
        project_id=project_id,
        capability_keys=[key for key, _tab in CAPABILITY_TABS],
    )
    tabs = []
    for capability_key, tab in CAPABILITY_TABS:
        entry = states[capability_key]
        tabs.append(
            {
                "capability_key": capability_key,
                "tab": tab,
                "availability": entry["availability"],
                "state": entry["state"],
                "open": capability_is_active(entry["state"]),
            }
        )
    return tabs


def _open_issue_summary(conn, project_id: str, datastream_id: str) -> dict[str, Any] | None:
    """The SAME object the fleet badge reads, for one Datastream -- story 59.2.

    It calls the fleet's own reader and takes this Datastream's entry rather than
    writing a second query: two readings of one fact diverge at the first clause
    one of them grows, and the badge on the list and the badge on the header must
    never disagree about the same flux.

    NOT DERIVED FROM `_run_anomalies`. That one is per-run and filters
    `execution_id IS NOT NULL` by design; a header count derived from it would
    read zero for every issue whose run could not be named, which is the majority
    case on both databases.

    `None` when the store could not be read: the key is then absent from the
    header and the console says the count could not be read, never `0`.

    The swallow is `read_open_issue_counts`'s and it rolls back to a SAVEPOINT, so
    a failed aggregate leaves this connection usable -- the header is composed
    from a record already read, and `_capability_tabs` still has a statement to
    run after this one.
    """

    from core.dq_governance import (  # noqa: PLC0415
        NO_ISSUE_SUMMARY,
        read_open_issue_counts,
    )

    summaries = read_open_issue_counts(
        conn,
        project_id=project_id,
        # Arbitrage 1, and the same answer as the fleet: acknowledged is
        # reviewed, so the badge moves when someone marks it.
        include_acknowledged=False,
    )
    if summaries is None:
        return None
    return summaries.get(datastream_id, NO_ISSUE_SUMMARY)


def read_workbench(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    base = _read_base_record(conn, project_id, datastream_id)
    header = compose_header(base)
    header["capability_tabs"] = _capability_tabs(conn, project_id)
    # WHAT A ROLE CHANGE WOULD DO, BEFORE IT IS ASKED FOR -- amendment 9 of the
    # 2026-08-11 review, delivered 2026-08-31.
    #
    # On the HEADER, for the same reason the lifecycle menu is: the role is a
    # property of the Datastream as an object, not of any one of its eight
    # readings, and the control that changes it sits beside rename and archive.
    #
    # It is served rather than composed in the browser because the consequence is
    # a PAIR -- the connector manifest's category and the role -- resolved by the
    # same table the fee ladder itself reads (`fee_tax_source_types._AGREEMENT`).
    # A console that re-spelled that table would be a second authority on which
    # Datastreams the cost cascade contains.
    from core.datastream_data_role import read_role_change  # noqa: PLC0415

    header["data_role_change"] = read_role_change(
        conn, project_id=project_id, datastream_id=datastream_id, record=base
    )
    # Story 59.2, arbitrage 3: it travels on the header every tab already loads,
    # so the badge is visible from the six tabs without a second request and
    # without a prop on `ObjectHeader`, which twelve files mount.
    summary = _open_issue_summary(conn, project_id, datastream_id)
    if summary is not None:
        header["open_issues"] = summary
    return header


#: The dated revisions of a plan, newest first -- the plan's versions ARE this
#: Datastream's dated imports (ratified 2026-08-24). Ordered by number and not by
#: `created_at`: two imports on one day are two revisions, and a date alone
#: cannot say which of them came second.
_PLAN_VERSIONS_SQL = """
    SELECT id, version_number, status, is_active, source_note, created_by, created_at
    FROM app.media_plan_versions
    WHERE plan_id = %s
    ORDER BY version_number DESC
"""


def _media_plan_evidence(
    conn, project_id: str, datastream_id: str, base: dict[str, Any]
) -> dict[str, Any] | None:
    """What media plan this Datastream carries -- ``None`` when it carries none.

    ``None`` AND NOT AN EMPTY BLOCK, because a Datastream that pulls from an
    advertising API has no relationship with media plans at all: rendering a
    panel that says "this is not a media plan" on every connector of the product
    would be a sentence nobody asked for. The block exists only for a Datastream
    whose Template
    DECLARES the plan-store landing -- the same declaration `import_runner`
    routes on, read from the same sealed contract, so "is this a carrier" has one
    answer in the product and not two.

    Ratified 2026-08-24 (`file-source-ingestion.md`, `analyze-and-test.md`): a
    plan is created and its dated revisions imported in the carrier's Workbench.
    This is the evidence that screen reads.
    """
    if str((base or {}).get("source_kind") or "") != "managed_feed":
        return None

    from core.file_source_resolution import (  # noqa: PLC0415
        resolve_file_source_producer,
    )
    from core.file_source_template import LANDING_PLAN_STORE  # noqa: PLC0415

    # NOT WRAPPED IN A `try`, and that is this module's rule rather than an
    # oversight: a failed statement leaves the Postgres transaction aborted, so a
    # private swallow here would break whatever the tab read NEXT while looking
    # like resilience. `resolve_file_source_producer` is documented as returning
    # `None` -- never raising -- for a Datastream that carries no template
    # binding, which is the only case this read has to survive.
    producer = resolve_file_source_producer(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        mapping_version_id=(base or {}).get("current_mapping_version_id"),
    )

    contract = (getattr(producer, "template", None) or {}).get("contract") or {}
    if contract.get("landing_target") != LANDING_PLAN_STORE:
        return None

    from core.mediaplan_store import get_carrier_plan  # noqa: PLC0415

    plan = get_carrier_plan(conn, datastream_id=datastream_id)
    if plan is None:
        return {
            "carrier": True,
            "plan": None,
            "versions": [],
            # THE EMPTINESS SAYS WHY, AND THE GESTURE IS ON THIS TAB. It is not a
            # deployment state and not a table name: what is missing is the plan
            # itself, and creating it is the control beside this sentence.
            "empty_message": "This Datastream carries no media plan yet",
            "empty_reason": (
                "Its template reads a spreadsheet as plan lines, so the file it "
                "receives becomes a media plan rather than warehouse rows. Name "
                "the plan here first; every file that arrives afterwards lands as "
                "a new dated version of it."
            ),
        }

    with conn.cursor() as cur:
        cur.execute(_PLAN_VERSIONS_SQL, (plan["id"],))
        versions = _rows(cur)

    return {
        "carrier": True,
        "plan": {
            "id": plan["id"],
            "name": plan["name"],
            "currency": plan["currency"],
            "created_at": plan["created_at"],
        },
        "versions": versions,
        # WHY A REVISION IS AN IMPORT AND NOT AN EDIT. Said once, here, because
        # this is the only screen where somebody sends the next file.
        "revision_reason": (
            "A revision of this plan is a new file imported here: it lands BESIDE "
            "the versions below, never over them, so yesterday's plan stays "
            "readable as of yesterday."
        ),
        # A LANDED VERSION IS A CANDIDATE, AND SAYING SO IS NOT A DETAIL. The plan
        # store publishes by its own explicit act (`import_runner` refuses a
        # plan-store import that publishes); a screen that let a person believe an
        # import had taken effect would be describing a state nobody reached.
        "candidate_reason": (
            "An imported version is a candidate until it is published. The reading "
            "in Analyze uses the published version."
        ),
        "no_version_message": "No file has been imported into this plan yet",
    }


def _stage_evidence(conn, project_id: str, datastream_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,execution_id,stage,phase_state,occurred_at,interval_start,interval_end,
                      plan_version_id,mapping_version_id,projection_version_ref,artifact_ref,
                      materialization_ref,schema_hash,profile_evidence,coverage_evidence,
                      row_count,grain_evidence,safe_error,evidence
                 FROM app.datastream_execution_stage_evidence
                WHERE project_id=%s AND datastream_id=%s
                ORDER BY occurred_at DESC,stage""",
            (project_id, datastream_id),
        )
        return _rows(cur)


def _phase_evidence(conn, project_id: str, datastream_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,execution_id,phase,phase_state,occurred_at,interval_start,
                      interval_end,plan_version_id,mapping_version_id,trace_ref,
                      artifact_ref,row_count,quota_evidence,safe_error,evidence
                 FROM app.datastream_execution_phase_evidence
                WHERE project_id=%s AND datastream_id=%s
                ORDER BY occurred_at,phase""",
            (project_id, datastream_id),
        )
        return _rows(cur)


def _step_evidence(conn, project_id: str, datastream_id: str) -> dict[str, list[dict[str, Any]]]:
    """The timed spans of the four ratified steps, per run -- story 58.10.

    A SECOND VOCABULARY IS NOT READ HERE, ON PURPOSE.
    `datastream-workbench-and-wizard.md` ratifies that the four steps
    `Collect`/`Map`/`Check`/`Publish` and the seven phases of
    `app.datastream_execution_phase_evidence` are two vocabularies and stay two,
    so this reads migration 223's own table and folds nothing onto anything.

    ``ended_at`` travels as it is stored. The duration is derived only when BOTH
    ends exist, and is `null` otherwise -- never `0`. Whether a missing end
    means "still working" is NOT decided here: it is decided by the run's own
    state, in :func:`_mark_step_spans`, because the span alone cannot know.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT execution_id,step,started_at,ended_at
                 FROM app.datastream_execution_step_evidence
                WHERE project_id=%s AND datastream_id=%s
                ORDER BY started_at,step""",
            (project_id, datastream_id),
        )
        spans = _rows(cur)
    by_execution: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        started, ended = span.get("started_at"), span.get("ended_at")
        span["duration_seconds"] = (
            (ended - started).total_seconds()
            if started is not None and ended is not None
            else None
        )
        by_execution.setdefault(str(span.pop("execution_id")), []).append(span)
    return by_execution


def _mark_step_spans(spans: list[dict[str, Any]], run_state: Any) -> list[dict[str, Any]]:
    """Say, per span, whether the step is REALLY still working -- story 58.10.

    THE AUTHORITY IS THE RUN'S STATE, NOT THE ABSENCE OF AN END, and that is
    the whole repair. An open span means "still running" only while the run
    itself is running. Measured 2026-08-07: `commit_publication` advanced
    `ready -> publishing -> published` with its own two `UPDATE`s and never
    called `advance_state`, so the close written there never ran for the
    dominant successful path; `datastream_activation`, `reconcile_execution`,
    `_reconcile_fail_closed` and `begin_managed_file_promotion` set a state the
    same way. Probed: a run reached `published` with its `Collect` span still
    `ended_at NULL`.

    AI-223 has since converted all six of those private `UPDATE`s: every state
    change now crosses `datastream_publication.advance_state`, which closes the
    spans, and `tests/conformance/test_one_state_machine_for_a_run.py` refuses
    the seventh writer. THIS DERIVATION STAYS ANYWAY, and it is not redundant:
    it answers for the runs written BEFORE that conversion, and for a crash
    between the state write and the span close. A writer can be made to close a
    span; a writer cannot be made not to die. Deciding it HERE is true by
    construction for every path, present and past, because a reader cannot
    bypass the reader.

    `execution_states.TERMINAL_STATES` is the one list, mirrored on the console
    by `executionStates.ts` and compared entry by entry by
    `tests/conformance/test_execution_state_registry.py`.
    """
    moving = not execution_states.is_terminal(run_state)
    # A NEW dict per span, never a mutation of the row read from the database.
    # `still_running` is the only field of this payload whose value depends on
    # something OUTSIDE the span, so a shared row marked in place would carry
    # one run's answer onto another's.
    return [
        # `still_running` is a THIRD answer, distinct from the duration: a span
        # with no end on a terminal run is `null` duration AND `False` here,
        # which is what makes a screen say "entered, not timed" instead of
        # either "0 s" or "still working".
        {**span, "still_running": span.get("ended_at") is None and moving}
        for span in spans
    ]


#: The issue columns the run block reads. `root_cause_fingerprint` is NOT among
#: them: it is how the replay recovers the field server-side, and a console has
#: no use for a hash it cannot invert.
_ISSUE_COLUMNS = (
    "id",
    "execution_id",
    "monitor_id",
    "status",
    "severity",
    "first_seen_at",
    "last_seen_at",
)


def _run_anomalies(conn, project_id: str, datastream_id: str) -> dict[str, Any]:
    """What each run of this Datastream was found to carry -- stories 58.10, 59.1.

    Readable at all because migration 222 gave `app.dq_issues` and
    `app.dq_evaluations` a `datastream_id` and an `execution_id`; before it, the
    only link to a flux was `dq_monitors.target_id`, at the grain of the
    Datastream and never of the run.

    FOUR FACTS, FOUR SENTENCES, and they are four because collapsing any two
    makes the screen answer a question nobody asked:

    * issues under this run -- the detail, unfolded on demand;
    * no issue and some evaluation under this run -- checked, nothing found;
    * no evaluation naming this run -- no monitor evaluated ON THIS RUN, which is
      literally true of the run and says nothing about the flux;
    * evaluations of this flux that could NAME no run -- counted at the grain of
      the DATASTREAM (`evaluations_without_run`), because that is the grain the
      fact has. Rendering it under a run would attribute to a collection an
      evaluation that named none, which is the more expensive of the two
      defects. `app.pull_jobs.execution_id` is what would fill it, and it was
      NULL on 130 rows of 130 (disposable base) and 6 of 6 (preprod) when this
      was written -- so this is the MAJORITY case, not an edge one.

    STORY 59.1 ADDS NO SECOND COUNT. The issue count IS the length of the detail
    read here: two `SELECT`s on `app.dq_issues` in one handler would be two
    readings of one thing, and they would disagree the first time one of them
    grew a clause. The `WHERE` is the exact predicate of `idx_dq_issues_execution`
    (migration 222).

    The monitor read is a THIRD statement on a THIRD table and it is skipped
    outright when no issue was found -- which is every Datastream of both bases
    today.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {",".join(_ISSUE_COLUMNS)},root_cause_fingerprint
                  FROM app.dq_issues
                 WHERE project_id=%s AND datastream_id=%s AND execution_id IS NOT NULL
                 ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'degrading' THEN 1
                          ELSE 2 END,last_seen_at DESC""",  # noqa: S608 - columns are literals
            (project_id, datastream_id),
        )
        issues = _rows(cur)

    profiles = _issue_profiles(conn, project_id, {str(row["monitor_id"]) for row in issues})
    runs: dict[str, dict[str, Any]] = {}
    for issue in issues:
        entry = runs.setdefault(
            str(issue["execution_id"]), {"anomalies": 0, "evaluations": 0, "issues": []}
        )
        profile = profiles.get(str(issue["monitor_id"]), {})
        entry["issues"].append(
            {key: issue[key] for key in _ISSUE_COLUMNS if key != "execution_id"} | profile
        )
        entry["anomalies"] = len(entry["issues"])

    # ONE reading of `app.dq_evaluations`, and it is NOT filtered by
    # `execution_id`: the same GROUP BY answers the per-run counts and the
    # Datastream-grain one, whose bucket is the NULL key.
    without_run = 0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT execution_id,count(*) AS total
                 FROM app.dq_evaluations
                WHERE project_id=%s AND datastream_id=%s
                GROUP BY execution_id""",
            (project_id, datastream_id),
        )
        for row in _rows(cur):
            total = int(row["total"] or 0)
            if row["execution_id"] is None:
                without_run = total
                continue
            entry = runs.setdefault(
                str(row["execution_id"]), {"anomalies": 0, "evaluations": 0, "issues": []}
            )
            entry["evaluations"] = total
    return {"runs": runs, "evaluations_without_run": without_run}


def _issue_profiles(conn, project_id: str, monitor_ids: set[str]) -> dict[str, dict[str, Any]]:
    """The monitor's label, and what its PROFILE declares it can replay.

    The console holds no list of profile names, and this is why: whether an issue
    has faulty rows to show travels with the issue, decided by
    `dq_issue_rows.REPLAY_PROFILES` -- one entry per `dq_monitors.CHECK_PROFILES`
    key. A list typed on the screen would give the eighth profile the wrong
    default in silence.
    """
    if not monitor_ids:
        return {}
    from core.dq_issue_rows import profile_replay  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.id,m.label,v.check_profile
                 FROM app.dq_monitors m
                 LEFT JOIN app.dq_monitor_versions v
                   ON v.id=m.current_version_id AND v.monitor_id=m.id
                WHERE m.project_id=%s AND m.id=ANY(%s)""",
            (project_id, sorted(monitor_ids)),
        )
        found = _rows(cur)
    profiles = {
        str(row["id"]): {
            "monitor_label": row["label"],
            **profile_replay(row["check_profile"]).as_payload(),
        }
        for row in found
    }
    # A monitor row that could not be read is NAMED, never filled with a
    # plausible label: `profile_replay(None)` answers `unknown_check_profile`.
    for monitor_id in monitor_ids:
        profiles.setdefault(
            monitor_id, {"monitor_label": None, **profile_replay(None).as_payload()}
        )
    return profiles


def _run_duration_seconds(run: dict[str, Any]) -> float | None:
    """How long the run took, or None -- story 58.10, arbitrage 3.

    ``state_changed_at - started_at``, and both must exist: `started_at` is
    written by `open_collection_run` (migration 218) and every run minted before
    it carries none. Measured on the disposable base 2026-08-07: 103 of 428 runs
    can answer this. The other 325 answer `null`, which the screen turns into a
    sentence -- the same treatment `rows_written` already gets.

    Both ends are SERVER instants, so this is not the arithmetic story 63.4
    refuses: that one subtracted a server timestamp from the browser's clock.
    """
    started, ended = run.get("started_at"), run.get("state_changed_at")
    if started is None or ended is None:
        return None
    try:
        seconds = (ended - started).total_seconds()
    except TypeError:
        return None
    return seconds if seconds >= 0 else None


def _recovery_options(
    conn,
    *,
    base: dict[str, Any],
    run: dict[str, Any],
    interval: dict[str, Any] | None,
) -> dict[str, Any]:
    state = str(run.get("state") or "").lower()
    if state == "outcome_unknown":
        return {
            "eligible": True,
            "kinds": ["reconcile"],
            "interval": interval,
            "reviews": {},
            "reason": "Durable outcome must be reconciled before another operation",
        }
    # Recovery is offered for a run that ENDED BADLY. Derived, not listed: the
    # literal set here knew `failed/cancelled/partial/drifted`, so a run that
    # ended `collected` would have been fine by luck rather than by rule -- and
    # the next adverse state added would have silently had no drawer.
    if state not in execution_states.ADVERSE_STATES:
        return {
            "eligible": False,
            "kinds": [],
            "interval": interval,
            "reviews": {},
            "reason": "No adverse execution state",
        }

    from core.bounded_recovery import BoundedRecoveryError, assemble_proposal  # noqa: PLC0415

    source_kind = str(base.get("source_kind") or "")
    candidate_kinds = ["reprocess"]
    if source_kind == "connector_pull":
        candidate_kinds = ["synchronize", "reload", "reprocess"]
    reviews: dict[str, Any] = {}
    refusals: dict[str, str] = {}
    for kind in candidate_kinds:
        if kind == "reload" and not isinstance(interval, dict):
            refusals[kind] = "The selected execution has no exact half-open interval"
            continue
        try:
            proposal = assemble_proposal(
                conn,
                org_id=str(base.get("org_id") or ""),
                datastream_id=str(base["id"]),
                kind=kind,
                actor="workbench-eligibility",
                reason=f"Recover selected execution {run.get('id')}",
                date_from=str(interval.get("from") or "")[:10]
                if isinstance(interval, dict)
                else None,
                date_to_exclusive=str(interval.get("to_exclusive") or "")[:10]
                if isinstance(interval, dict)
                else None,
                partition=str(interval.get("partition") or "") or None
                if isinstance(interval, dict)
                else None,
                chosen_mapping_version_id=str(run.get("mapping_version_id") or "") or None,
            )
            reviews[kind] = proposal.as_insert_payload()
        except (BoundedRecoveryError, ValueError) as exc:
            refusals[kind] = str(exc)
    kinds = list(reviews)
    return {
        "eligible": bool(kinds),
        "kinds": kinds,
        "interval": interval,
        "reviews": reviews,
        "refusals": refusals,
        "reason": "Server preflight passed" if kinds else "No bounded recovery preflight passed",
    }


def _cleanup_rule_chain(conn, project_id: str, datastream_id: str) -> dict:
    """The cleanup rules that reach this Datastream, for the Processing chain.

    Story 60.3. Three states and three sentences, because the tab must not read
    "no cleanup rule" when the truth is "the store could not be read":
      * `available` with the rules;
      * `empty` -- the store answered and there are none;
      * `unavailable` with a reason, and no list of any kind.

    The rules themselves are edited in Governance and nowhere else
    (`datastream-workbench-and-wizard.md:989`), so this projection carries no
    write handle: a name, a plain-language condition, a reach and an enabled flag.

    Amended by story 60.6: the fail-soft moved into `cleanup_rules`, where it is
    a SAVEPOINT and not a `try/except`. A swallowed statement error here left the
    transaction aborted for `_capability_projection`, read two lines below on the
    same connection -- the measured failure `dq_governance.read_open_issue_counts`
    documents, and the reason this module carries no private swallow.
    """
    from core.cleanup_rules import read_datastream_chain  # noqa: PLC0415

    rules = read_datastream_chain(conn, project_id=project_id, datastream_id=datastream_id)
    if rules is None:
        return {
            "state": "unavailable",
            "rules": [],
            "reason": (
                "The cleanup rules of this Project could not be read, so the "
                "transformation step of this chain is unknown. This is not an "
                "absence of rules."
            ),
        }
    return {
        "state": "available" if rules else "empty",
        "rules": rules,
        "reason": None,
    }


def _fields_named_elsewhere(conn, project_id: str, datastream_id: str) -> dict[str, int]:
    """`{nom de champ: combien d'AUTRES flux actifs le portent}`.

    LA DETECTION DE CE QUI EST COMMUN NE SE FAIT PAS DEPUIS UN SEUL FLUX. `date`
    vit dans sept des neuf flux du projet de derivation, et personne ne pouvait le
    voir depuis l'interieur de l'un d'eux -- l'ecran proposait d'epingler une
    colonne sans dire que huit autres nommaient la meme chose. C'est pourtant
    exactement ce qui decide si une cle commune vaut la peine d'etre declaree.

    Le compte porte sur le NOM du champ, pas sur son epinglage : un flux qui n'a
    pas encore ete epingle compte quand meme. Attendre l'epinglage pour compter
    rendrait le premier geste invisible, et c'est le premier qui coute.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.mapping_payload
              FROM app.datastreams d
              JOIN app.datastream_mapping_versions v ON v.id = d.current_mapping_version_id
             WHERE d.project_id = %s AND d.lifecycle_state = 'active' AND d.id <> %s
            """,
            (project_id, datastream_id),
        )
        rows = _rows(cur)
    counts: dict[str, int] = {}
    for row in rows:
        payload = _json(row.get("mapping_payload"), {}) or {}
        seen = {
            str(field.get("field_id") or field.get("name") or "").strip().lower()
            for field in (payload.get("fields") or [])
            if isinstance(field, dict)
        }
        for name in seen:
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def _identity_candidates(
    conn,
    project_id: str,
    datastream_id: str,
    versions: list[dict[str, Any]],
    active_version_id: str | None,
) -> dict[str, Any]:
    """What this mapping could name as a SHARED identity, and what that unlocks.

    THE MEASUREMENT THAT ASKED FOR THIS, 2026-08-13: the nine published
    Datastreams of the derivation project carried ZERO field bound to the MDM,
    with every binding at `status: confirmed`. A mapping can therefore be
    confirmed without naming one shared identity -- "confirmed" says a person
    looked, not that two feeds speak of the same day. No cross was possible, and
    the person who found out was the one asking the question.

    THE PROPOSAL IS DERIVED FROM THE NAME, AND NEVER WRITTEN. A field called
    `date` and the canonical field `date` carry the same word; the server sees
    it, offers it, a person confirms. Guessing on their behalf would write an
    identity nobody declared; offering nothing makes them open a selector per
    column, nine times, over nine feeds.

    IT IS COMPUTED HERE BECAUSE THE VOCABULARY LIVES HERE. A match resolved in
    the console would be a second authority over the vocabulary, free to diverge
    the day a scope changes -- the same pair of copies story 59.5 spent a day
    repairing.
    """
    from core.canonical_field_registry import list_visible_canonical_fields  # noqa: PLC0415

    #  LA VERSION EN VIGUEUR, pas la plus recente. Lire la derniere ligne de la
    #  liste faisait proposer d'apres une version CANDIDATE que le pointeur
    #  n'atteint pas : l'ecran annoncait « rien a epingler » sur un flux dont la
    #  version active ne porte aucun lien. « Enregistre » n'est pas « en vigueur »,
    #  et c'est ce que tout le reste du produit lit.
    payload: dict[str, Any] = {}
    for version in versions:
        if str(version.get("id") or "") == str(active_version_id or ""):
            payload = _json(version.get("mapping_payload"), {}) or {}
            break

    #  NO `except` HERE, and the module-wide rule is why: a read that swallowed
    #  its failure would answer "no identity to propose" for "I could not read
    #  the vocabulary", which is the false zero this module is forbidden to
    #  produce (`test_datastream_workbench.py`). An unreadable vocabulary fails
    #  the tab read, loudly, where a person can see it.
    vocabulary = list_visible_canonical_fields(conn, project_id=project_id)

    by_name = {str(entry["canonical_name"]).strip().lower(): entry for entry in vocabulary}
    shared = _fields_named_elsewhere(conn, project_id, datastream_id)
    present_names = {
        str(field.get("field_id") or field.get("name") or "").strip().lower()
        for field in (payload.get("fields") or [])
        if isinstance(field, dict)
    }
    proposed: list[dict[str, Any]] = []
    bound = 0
    unbound = 0
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        if binding.get("status") == "excluded":
            continue
        name = str(field.get("field_id") or field.get("name") or "").strip()
        if not name:
            continue
        if binding.get("mdm_target"):
            bound += 1
            continue
        unbound += 1
        match = by_name.get(name.lower())
        if match is None:
            continue
        proposed.append(
            {
                "field_id": name,
                "canonical_field_id": match["id"],
                "canonical_name": match["canonical_name"],
                "concept_kind": match["concept_kind"],
                "scope": match["scope"],
                # The match is EXACT and says so. A fuzzy one would need a score,
                # and a score invites a threshold nobody can defend.
                "match": "exact_name",
                # HOW MANY OTHER FEEDS NAME THE SAME THING -- detected, not
                # declared. A shared identity is not a property of one mapping:
                # `date` lives in seven of the nine feeds of the derivation
                # project, and nobody could see it from inside one of them. This
                # is the number that turns "pin this column" into "pin what eight
                # feeds already have in common", which is the whole point of the
                # gesture.
                "also_named_by": shared.get(name.lower(), 0),
            }
        )
    #  DE QUEL OBJET CE FLUX PARLE-T-IL ? Une date est un AXE, pas une identite :
    #  un flux qui ne porte qu'elle ne se croisera jamais que sur le calendrier --
    #  on saura que quelque chose s'est passe ce jour-la, jamais quoi. Mesure du
    #  2026-08-14 : « video publications » porte une seule colonne, `date`, alors
    #  que le flux voisin porte `video` ; dater la sortie d'une video PRECISE y
    #  est impossible, et aucune cle commune ne rattrape une colonne absente.
    #
    #  La question se pose pour TOUT flux, pas pour celui-la : le vocabulaire dit
    #  quels champs canoniques nomment un objet (`object_kind`), et ce sont eux
    #  qui rendent un croisement possible au-dela du calendrier.
    named_objects = sorted(
        {
            str(entry["object_kind"])
            for entry in vocabulary
            if entry.get("object_kind")
            and str(entry["canonical_name"]).strip().lower() in present_names
        }
    )

    #  ET LE VERDICT NE SE PRONONCE QUE SI LE VOCABULAIRE PEUT REPONDRE.
    #
    #  Mesure du 2026-08-16, sur une base ou tout le vocabulaire de plateforme est
    #  provisionne : 272 champs canoniques, **0** portent un `object_kind`. Et ce
    #  n'est pas un oubli -- `governance.md` l'ecrit comme une regle : la colonne
    #  vient « de personne », parce qu'« un champ qui qualifie un objet est une
    #  declaration que quelqu'un fait, pas une valeur derivee d'un type ».
    #
    #  Donc `not named_objects` etait VRAI pour tout flux de toute instance, et le
    #  panneau d'identites -- celui qui existe pour faire epingler -- rendait
    #  « ce flux ne nomme aucun objet » et n'offrait JAMAIS l'epinglage. Un ecran
    #  qui refuse toujours ne refuse rien : il est absent.
    #
    #  « Aucun objet nomme » et « personne n'a encore dit quels champs nomment un
    #  objet » sont deux faits opposes qui s'ecrivaient tous les deux `[]`. C'est
    #  le faux zero que ce module a interdiction de produire, deux paragraphes
    #  plus haut. Le second se dit maintenant, et il ne se dit PAS comme le
    #  premier : deriver un `object_kind` d'un type de donnee serait inventer la
    #  decision de conception que le document refuse explicitement.
    vocabulary_qualifies_objects = any(entry.get("object_kind") for entry in vocabulary)
    return {
        "proposed": proposed,
        "bound": bound,
        "unbound": unbound,
        "named_objects": named_objects,
        #: `known` : le vocabulaire qualifie des objets, donc l'absence en est une.
        #: `undeterminable` : aucun champ canonique ne porte d'`object_kind`, donc
        #: la question n'a pas de repondant -- ce n'est pas une reponse negative.
        "named_objects_state": "known" if vocabulary_qualifies_objects else "undeterminable",
        "calendar_only": vocabulary_qualifies_objects and not named_objects,
        # What the pins would unlock, stated with the gesture rather than after
        # it: a person who does not see the effect does not repeat the gesture on
        # the next feed.
        "unlocks": _crossings_unlocked(
            conn, project_id, datastream_id, [entry["canonical_field_id"] for entry in proposed]
        ),
    }


def _crossings_unlocked(
    conn, project_id: str, datastream_id: str, would_pin: list[str]
) -> dict[str, Any]:
    """Which common keys this feed would join, and with which other feeds.

    A common key is satisfied by a feed when EVERY component of its current
    version is named by that feed -- an ordered key half-named is not a key.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.name, v.id, v.components
              FROM app.mdm_common_keys k
              JOIN app.mdm_common_key_versions v ON v.id = k.current_version_id
             WHERE k.project_id = %s AND k.status = 'active'
             ORDER BY k.name
            """,
            (project_id,),
        )
        keys = _rows(cur)
        cur.execute(
            """
            SELECT d.id, d.name, v.mapping_payload
              FROM app.datastreams d
              JOIN app.datastream_mapping_versions v ON v.id = d.current_mapping_version_id
             WHERE d.project_id = %s AND d.lifecycle_state = 'active' AND d.id <> %s
            """,
            (project_id, datastream_id),
        )
        others = _rows(cur)

    def named_by(payload: Any) -> set[str]:
        fields = (_json(payload, {}) or {}).get("fields") or []
        return {
            str(((field.get("binding") or {}).get("mdm_target")))
            for field in fields
            if isinstance(field, dict) and (field.get("binding") or {}).get("mdm_target")
        }

    mine = set(would_pin)
    unlocked: list[dict[str, Any]] = []
    for key in keys:
        components = {
            str(component.get("canonical_field_id"))
            for component in (_json(key.get("components"), []) or [])
            if isinstance(component, dict) and component.get("canonical_field_id")
        }
        if not components or not components <= mine:
            continue
        partners = [
            {"datastream_id": other["id"], "name": other["name"]}
            for other in others
            if components <= named_by(other.get("mapping_payload"))
        ]
        if partners:
            unlocked.append(
                {
                    "common_key": key.get("name"),
                    "common_key_version_id": key.get("id"),
                    "with": partners,
                }
            )
    return {"crossings": unlocked, "count": sum(len(entry["with"]) for entry in unlocked)}


def _measurement_grain_candidates(
    conn,
    project_id: str,
    datastream_id: str,
    versions: list[dict[str, Any]],
    active_version_id: str | None,
) -> dict[str, Any]:
    """A candidate measurement grain per metric this mapping BINDS to the MDM.

    THE MIRROR, ON THE MEASURE AXIS, OF `_identity_candidates` (story 71.3). Where
    that panel offers to PIN a shared identity, this derives -- never writes -- the
    grain a metric is reported at, from the columns already bound to canonical
    fields: the metric is the head, and the dimensions present beside it at the
    mapping's grain are its members (`governance.md`, *the fourth fact*).

    IT RIDES `binding.mdm_target`, AND ONLY THAT. Governance owns the canonical
    grain; the Datastream is where a person states it against the columns in front
    of them, so a column not bound to the MDM cannot enter a grain -- a metric or a
    dimension with no `mdm_target` is skipped, because the binding is the wire and
    there is nothing else to name a canonical field by.

    NO METRIC BOUND MEANS NO CANDIDATE, and that is an honest empty, not a zero
    that hides an unread mapping: the vocabulary is read WITHOUT an `except`, so an
    MDM that cannot be read fails the tab loudly rather than answering "no grain to
    declare" -- the false zero this module is forbidden to produce.
    """
    from core.canonical_field_registry import list_visible_canonical_fields  # noqa: PLC0415

    #  LA VERSION EN VIGUEUR, comme `_identity_candidates` : un grain se confirme
    #  contre ce que le flux publie, pas contre une version candidate que le
    #  pointeur n'atteint pas.
    payload: dict[str, Any] = {}
    for version in versions:
        if str(version.get("id") or "") == str(active_version_id or ""):
            payload = _json(version.get("mapping_payload"), {}) or {}
            break

    vocabulary = {
        str(entry["id"]): entry
        for entry in list_visible_canonical_fields(conn, project_id=project_id)
    }

    heads: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    #: A binding whose `mdm_target` names a field this Project cannot see
    #: (archived, foreign, unreadable). It backs no grain, so it is neither head
    #: nor member -- counted so the panel names the binding to repair rather than
    #: dropping it in silence.
    bound_unresolved = 0
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        if binding.get("status") == "excluded":
            continue
        target = binding.get("mdm_target")
        if not target:
            continue  # not bound to the MDM -- cannot enter a grain
        canonical = vocabulary.get(str(target))
        if canonical is None:
            bound_unresolved += 1
            continue
        column = str(field.get("field_id") or field.get("name") or "").strip()
        entry = {
            "field_id": column,
            "canonical_field_id": str(target),
            "canonical_name": str(canonical.get("canonical_name") or ""),
            "value_type": str(canonical.get("value_type") or ""),
        }
        kind = canonical.get("concept_kind")
        if kind == "metric":
            heads.append(entry)
        elif kind == "dimension":
            members.append(entry)

    #  UN CANDIDAT PAR METRIQUE BINDEE, chacune rapportee contre TOUTES les
    #  dimensions bindees presentes -- le grain est l'ensemble des dimensions a
    #  cote de la mesure, pas un sous-ensemble par metrique. La meme liste ordonnee
    #  chevauche donc chaque tete. Une metrique sans aucune dimension reste un
    #  candidat legal (un total), la seule divergence d'avec la cle commune.
    candidates = [
        {"head": head, "members": members, "member_count": len(members)}
        for head in heads
    ]
    return {
        "candidates": candidates,
        "bound_metrics": len(heads),
        "bound_dimensions": len(members),
        "bound_unresolved": bound_unresolved,
    }


def _mdm_targets_of_current_mapping(
    conn, project_id: str, datastream_id: str
) -> set[str]:
    """The canonical field ids the CURRENT mapping version binds, excluded aside.

    Only the version in force: a grain is confirmed against what the Datastream
    actually publishes. An empty set when nothing is in force refuses every
    column -- correct, because a Datastream with no published mapping has bound
    nothing to the MDM.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.mapping_payload
              FROM app.datastreams d
              JOIN app.datastream_mapping_versions v
                    ON v.id = d.current_mapping_version_id
             WHERE d.project_id = %s AND d.id = %s
            """,
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    if row is None:
        return set()
    payload = _json(row[0], {}) or {}
    targets: set[str] = set()
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
        if binding.get("status") == "excluded":
            continue
        target = binding.get("mdm_target")
        if target:
            targets.add(str(target))
    return targets


def confirm_measurement_grain(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    name: Any,
    head_field_id: Any,
    member_field_ids: Sequence[Any],
    actor: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Confirm a candidate grain as a governed MDM version -- via the MDM (71.3).

    THE WORKBENCH'S OWN REFUSAL, BEFORE THE MDM'S. A grain declared here can only
    name a metric and dimensions THIS Datastream has bound to the MDM: the
    `mdm_target` binding is the wire, so a head or a member that no binding of the
    current mapping backs is refused HERE, before `create_measurement_grain` ever
    runs. The MDM then applies its own refusals -- a dimension head, a metric
    member, an archived or foreign field, a duplicate. This guard is not a
    substitute for them: it is the half the mapping owns, that the metric-dimension
    link a person names is one the columns in front of them actually carry.

    The declaration references MDM canonical fields on both sides, so it is *via
    the MDM* and not a private per-Datastream model. The caller owns the
    transaction, exactly like `create_measurement_grain`.
    """
    from core.metric_dimensions import (  # noqa: PLC0415
        MeasurementGrainRefused,
        create_measurement_grain,
    )

    backed = _mdm_targets_of_current_mapping(conn, project_id, datastream_id)

    head_id = head_field_id.strip() if isinstance(head_field_id, str) else ""
    if not head_id or head_id not in backed:
        raise MeasurementGrainRefused(
            "grain_head_not_bound_to_mdm",
            "The measure a grain reports must be a column this Datastream has bound "
            "to a canonical field. Bind the metric column to the MDM on this "
            "mapping, then confirm the grain.",
        )
    wanted = list(member_field_ids) if isinstance(member_field_ids, (list, tuple)) else []
    for raw in wanted:
        member_id = raw.strip() if isinstance(raw, str) else ""
        if not member_id or member_id not in backed:
            raise MeasurementGrainRefused(
                "grain_member_not_bound_to_mdm",
                "Every dimension of a grain must be a column this Datastream has "
                "bound to a canonical field. Bind the dimension column to the MDM "
                "on this mapping, then confirm the grain.",
            )

    return create_measurement_grain(
        conn,
        project_id=project_id,
        name=name,
        head_field_id=head_id,
        member_field_ids=wanted,
        description=description,
        actor=actor,
    )


def _attach_source_reports(conn, project_id: str, datastream_id: str, versions) -> None:
    """Give every mapping version the REPORT its plan version names, in place.

    Story 27.8. A language binding is keyed `(connector, report_id, source_field)`
    -- the report is in the key because one column can carry the language OBSERVED
    on a person in one report and the language TARGETED in another. The Map tab is
    the surface `core.language_bindings_api` names for that gesture, and it already
    shows the column and the connector; it could not name the report, so the
    gesture could not be offered without asking a person to type an identifier.

    A version whose plan names no report gets NO `report_id` key rather than a
    `null`: an absent key is "this version pulls no named report", and a control
    that would need one is simply not offered.
    """
    plan_ids = sorted(
        {
            str(version.get("plan_version_id"))
            for version in versions or ()
            if isinstance(version, dict) and version.get("plan_version_id")
        }
    )
    if not plan_ids:
        return
    # NO `try/except` HERE, deliberately: this runs on the tab's own connection,
    # and a swallowed error would leave the transaction aborted for everything
    # read after it -- the exact rule
    # `test_the_header_count_is_the_fleets_own_reader_and_not_a_second_query`
    # enforces on this module. This is a plain read of a table the sibling branch
    # of the same function already opens; a failure here is a failure of the tab.
    reports: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, normalized_payload FROM app.datastream_plan_versions
                WHERE project_id=%s AND datastream_id=%s AND id = ANY(%s)""",
            (project_id, datastream_id, plan_ids),
        )
        for plan_id, payload in cur.fetchall():
            source = (_json(payload, {}) or {}).get("source")
            report_id = (source or {}).get("report_id") if isinstance(source, dict) else None
            if isinstance(report_id, str) and report_id.strip():
                reports[str(plan_id)] = report_id.strip()
    for version in versions or ():
        if not isinstance(version, dict):
            continue
        report_id = reports.get(str(version.get("plan_version_id")))
        if report_id:
            version["report_id"] = report_id


def _attach_column_treatments(
    conn, project_id: str, datastream_id: str, versions: list[dict[str, Any]]
) -> None:
    """Give every mapping version its per-column reading, in place.

    Story 60.6. One row per source column: the source name, WHAT IT BECOMES in
    one word, its canonical/MDM target, its confidence and an example value read
    from the sample already profiled into the payload. Nothing is fabricated --
    a column the sample left empty says `no sample value`, a column that was
    never profiled says `unknown`, and the two are not the same fact.

    A version whose reading could not be built gets NO `columns` key rather than
    an empty list: an empty list means "this version binds no field", which is a
    different sentence from "the column mapping could not be read", and the
    console renders them apart.
    """
    from core.column_treatments import ColumnTreatmentError, describe_columns  # noqa: PLC0415
    from core.value_mapping_tables import read_assigned_source_fields  # noqa: PLC0415

    # The fail-soft belongs to the module that OWNS the table, and it is a
    # SAVEPOINT rather than a `try/except`: a swallowed statement error leaves
    # the transaction aborted for the capability projection read right after.
    # `None` (the store could not be read) downgrades exactly one word --
    # `Resolved by a list` becomes `Direct` -- and never turns a column into
    # `Excluded`, which is the reading that decides whether data lands.
    assigned = read_assigned_source_fields(
        conn, project_id=project_id, datastream_id=datastream_id
    ) or frozenset()
    for version in versions:
        payload = _json(version.get("mapping_payload"), {})
        if not isinstance(payload, dict):
            continue
        try:
            version["columns"] = describe_columns(payload, resolved_by_list=assigned)
        except (ColumnTreatmentError, AttributeError, TypeError, ValueError):
            logger.warning(
                "workbench: column treatments unreadable ds=%s version=%s",
                datastream_id,
                version.get("id"),
            )


def _capability_projection(
    conn,
    project_id: str,
    datastream_id: str,
    *,
    blocks: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Project the shared capability read model onto one Workbench tab.

    ``blocks`` narrows the impact each tab carries so Mapping is not handed the
    quota evidence Processing owns. The projection is composed once on the server;
    the tab renders it and reconstructs nothing.
    """
    from core.capability_proposals import read_datastream_capabilities  # noqa: PLC0415

    projection = read_datastream_capabilities(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    if blocks is None:
        return projection
    narrowed = []
    for capability in projection["capabilities"]:
        impact = capability.get("impact") or {}
        narrowed.append(
            {**capability, "impact": {key: impact[key] for key in blocks if key in impact}}
        )
    return {**projection, "capabilities": narrowed}


def read_tab(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    tab: str,
    #: The request's query string, for the ONE tab whose reading is a choice.
    #:
    #: `placements` reads a media plan, and a Project carries N of them with none
    #: designated active (story 61.1, arbitrage A5). A second route taking the
    #: plan id in its path would have made two addresses for one tab, and the tab
    #: band can only navigate to one; a per-tab parameter here keeps the generated
    #: route contract intact. Every other tab ignores it.
    options: dict[str, str] | None = None,
) -> dict[str, Any]:
    base = _read_base_record(conn, project_id, datastream_id)
    if tab == "overview":
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.next_run_at,s.last_committed_watermark,s.retry_count,
                          s.missed_run_count,s.updated_at
                     FROM app.datastream_schedule_state s
                    WHERE s.project_id=%s AND s.datastream_id=%s
                      AND s.plan_version_id=%s""",
                (project_id, datastream_id, base.get("current_plan_version_id")),
            )
            schedule = _row(cur)
            cur.execute(
                # Story 63.1: the latest run carries WHERE IT IS, not only what
                # state it ended in. A run in flight was previously readable as
                # "loading" and nothing else, on every surface.
                """SELECT id,state,error_code,error_detail,row_count,state_changed_at,
                          plan_version_id,mapping_version_id,
                          step,day_in_progress,days_done,days_total,rows_written,
                          started_at,progress_updated_at
                     FROM app.datastream_executions
                    WHERE project_id=%s AND datastream_id=%s
                    ORDER BY created_at DESC LIMIT 1""",
                (project_id, datastream_id),
            )
            latest = _row(cur)
            cur.execute(
                """SELECT stage,COUNT(*) AS evidence_count,MAX(occurred_at) AS latest_at
                     FROM app.datastream_execution_stage_evidence
                    WHERE project_id=%s AND datastream_id=%s
                    GROUP BY stage ORDER BY stage""",
                (project_id, datastream_id),
            )
            coverage = _rows(cur)
            cur.execute(
                """SELECT COUNT(DISTINCT u.consumer_kind || ':' || u.consumer_ref)
                     FROM app.datastream_output_used_by u
                     JOIN app.datastream_outputs o ON o.id=u.output_id
                    WHERE o.project_id=%s AND o.datastream_id=%s""",
                (project_id, datastream_id),
            )
            downstream_count = int(cur.fetchone()[0])
            # WHAT IS PLACED ON THE `Check` STAGE — story 58.9, arbitrage 4.
            #
            # The four stages of the Overview colour on the OBJECTS placed on
            # them, never on `app.datastream_execution_stage_evidence`: that
            # table is empty across the whole of preprod, so a stage reading it
            # printed `0 / 4` for every Datastream — a zero where nothing was
            # measured. Three stages already have their object on this payload
            # (`schedule`, `mapping_health`, `downstream_count`); `Check` had
            # none, and this count is it.
            #
            # A PAYLOAD KEY, NOT A ROUTE. `screens/routes.json` stays at 427: the
            # tab a person already loads answers the question, and a second
            # address would be a second read of the same fact.
            cur.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE lifecycle_status='published')
                              AS published
                     FROM app.dq_monitors
                    WHERE project_id=%s AND target_kind='datastream'
                      AND target_id=%s""",
                (project_id, datastream_id),
            )
            monitor_total, monitor_published = cur.fetchone()
            # THE FOUR NAMED METRICS OF THIS TAB, WHICH NOTHING COMPOSED.
            #
            # The `Overview` row of the Workbench tab table asks for "cadence,
            # freshness and history coverage; latest run", and the validated
            # mockup leads with four headline numbers: freshness, run success
            # over 30 days, latest volume, next run. This query set answered
            # none of them, so no change to the tab alone could have surfaced
            # them — the fields were never on the wire.
            #
            # Success is counted over TERMINAL runs only: a run still `loading`
            # has neither succeeded nor failed, and counting it as a failure
            # would make an in-flight Datastream look broken every time someone
            # opened this tab.
            #
            # BOTH sets come from the registry. The literals that stood here read
            # `('published','failed','cancelled')` for the denominator and
            # `= 'published'` for the numerator, so from story 63.1 a successful
            # nightly collection left the denominator while a failed one entered
            # it and could never enter the numerator: the 30-day rate of every
            # Datastream that collects without publishing tended to 0%.
            cur.execute(
                """SELECT COUNT(*) FILTER (
                              WHERE state = ANY(%(terminal_states)s)
                          ) AS terminal,
                          COUNT(*) FILTER (
                              WHERE state = ANY(%(success_states)s)
                          ) AS succeeded
                     FROM app.datastream_executions
                    WHERE project_id=%(project_id)s AND datastream_id=%(datastream_id)s
                      AND created_at >= NOW() - INTERVAL '30 days'""",
                {
                    "project_id": project_id,
                    "datastream_id": datastream_id,
                    "terminal_states": list(execution_states.TERMINAL_STATES),
                    "success_states": list(execution_states.SUCCESS_STATES),
                },
            )
            terminal_count, succeeded_count = cur.fetchone()
            # Freshness and volume both describe the CURRENT truth, so both read
            # the latest run that actually published — not the latest run, which
            # may have failed and would date the data to its own failure.
            cur.execute(
                """SELECT state_changed_at, row_count
                     FROM app.datastream_executions
                    WHERE project_id=%s AND datastream_id=%s AND state='published'
                    ORDER BY state_changed_at DESC LIMIT 1""",
                (project_id, datastream_id),
            )
            published_row = cur.fetchone()
        evidence: Any = {
            "header": compose_header(base),
            "state": "available",
            "schedule": schedule,
            "latest_execution": latest,
            "stage_coverage": coverage,
            "downstream_count": downstream_count,
            # The `Check` stage's object. `count` is a MEASURED count, so `0` is
            # a real answer here — and the screen renders the invitation for it
            # rather than the digit, because "no check is placed" is what a
            # person acts on. An absent key is the other thing entirely: the
            # count could not be read, and the two never render for each other.
            # The live monitors take the bare word `published`: story 63.1 pinned
            # the longer spelling as one that must not reappear in this function,
            # because it named runs that were counted wrong. A count of monitors
            # is a different fact and takes a different word rather than
            # reopening that one.
            "dq_monitors": {
                "count": int(monitor_total or 0),
                "published": int(monitor_published or 0),
            },
            "run_success": {
                "window_days": 30,
                "terminal_count": int(terminal_count or 0),
                # `succeeded_count`, not `published_count`: since story 63.1 a
                # successful run may be `collected` rather than `published`, and
                # a key that says "published" while counting collected runs is a
                # lie on the wire. The console reads the new name.
                "succeeded_count": int(succeeded_count or 0),
            },
            "published_at": published_row[0] if published_row else None,
            "published_row_count": published_row[1] if published_row else None,
            # The fourth named thing. `blocking_count` is the actionable half —
            # it names the DISTANCE to an executable mapping, where `executable`
            # alone only says yes or no.
            "mapping_health": {
                "version_id": base.get("current_mapping_version_id"),
                # « AUCUNE VERSION » ET « AUCUNE EN VIGUEUR » NE SONT PAS LE MEME
                # FAIT. `version_id` est le POINTEUR actif : un Datastream en
                # brouillon a une version de mapping -- l'onglet Mapping la
                # montre et la laisse editer -- et l'Overview annoncait « rien ne
                # lie les champs a un sens canonique », ce qui est faux et
                # decourageant. Mesure de Jean, 2026-08-12, capture a l'appui.
                "proposed_version_id": base.get("proposed_mapping_version_id"),
                "executable": base.get("mapping_executable"),
                "blocking_count": base.get("mapping_blocking_count"),
            },
            # Story 48.1: capability applicability, coverage, reasons and the one
            # highest-priority repair -- server-composed, from the same read model
            # Project Settings and MCP use. No sixth tab, no seventh workspace.
            "capabilities": _capability_projection(conn, project_id, datastream_id),
        }
    elif tab == "data":
        stages = _stage_evidence(conn, project_id, datastream_id)
        observed = {str(item.get("stage")) for item in stages}
        # THE BOUNDED MASKED SAMPLE, WHICH THIS TAB EXISTS FOR.
        #
        # The `Data` row of the Workbench tab table contracts this tab as "stage
        # selector ... and bounded masked samples tied to exact run/plan/mapping
        # versions". `sample_state` was the literal `"unavailable"`, written
        # unconditionally, with a reason that asserted no sample had been
        # persisted — while `GET /api/datastreams/{id}/sample` has read exactly
        # that sample, masked and project-scoped, since story 12.19. The tab
        # could not do the one thing it is for, and no data would ever have
        # changed that: the answer was hardcoded.
        #
        # This says what it can prove and leaves the endpoint to answer
        # definitively. The endpoint owns the ambiguity rule (`fact_daily_kpi`
        # carries no Datastream discriminator, so two Datastreams on one
        # project/connector refuse rather than mix) and every honest refusal
        # underneath it — duplicating that here would give two answers that could
        # disagree.
        # ...AND WHY IT STILL CANNOT SHOW ONE.
        #
        # A first repair set this to `reachable` and wired a caller to
        # `GET /api/datastreams/{id}/sample`. That endpoint IS NOT MOUNTED:
        # `_datastream_sample` exists in `admin_api.py`, no `Route` registers it,
        # and `test_only_exact_workbench_rollback_routes_are_mounted` asserts its
        # absence on
        # purpose. Story 47.5 decommissioned it, and its reason is the whole
        # point — `:108` "cannot serve one processed table under several stage
        # labels or return an unbound sample", `:341` "reports no exact version
        # binding and aliases several requested stages".
        #
        # So it was retired for LYING about exactly the two things this tab
        # promises: the stage and the version binding. Re-mounting it would put
        # a known-dishonest surface back, and claiming `reachable` makes the
        # console call a 404. The honest answer is neither: say that no governed
        # sample endpoint exists yet, and name it as the blocker it is.
        #
        # `ui/admin/src/datastreams/workbench/DatastreamSample.tsx` is built,
        # tested and correct; it stays, unwired, because it is the trace of the
        # work that remains. What is missing is the REPLACEMENT endpoint — one
        # that binds a sample to its exact run/plan/mapping versions and refuses
        # to alias a stage. Story 47.5 does not name it; that is an owner call.
        connector = str((base or {}).get("module_name") or "").strip()
        # WHICH MODE IS BEING REFUSED, AND IN ITS OWN WORDS — amendment 7 of the
        # 2026-08-11 review, one block further down the same tab.
        #
        # `module_name` is NULL on `external_bq` and on `managed_feed` BY
        # CONSTRUCTION: `create_datastream` in `core/datastreams.py` sets it to
        # None for both and refuses a `connector_pull` without one. So the
        # sentence below is in practice aimed at PUSHED SOURCES alone — and it
        # told them they "declare no connector", a property of a mode they are
        # not in and can never enter. The FACT it states is true and measured:
        # `read_datastream_sample` and `read_datastream_export`
        # (`core/cache_warehouse.py`) both key `fact_daily_kpi` on the connector,
        # and the export raises `no_materialization` without one. Only the
        # vocabulary was borrowed from the other mode.
        mode = str((base or {}).get("source_kind") or "connector_pull")
        evidence = {
            "stages": stages,
            "availability": {
                stage: "available" if stage in observed else "unavailable"
                for stage in ("collected", "mapped", "processed", "published")
            },
            # WHY A STAGE IS NOT SELECTABLE, said here (story 58.3, arbitrage 8).
            #
            # `availability` reads ONE table --
            # `app.datastream_execution_stage_evidence` -- and the sentence below
            # exists because a disabled control that does not say why is the
            # defect the story's `Refuse` line names.
            #
            # LE CHIFFRE QUI JUSTIFIAIT CE BLOC A CHANGE, et le commentaire ne
            # l'avait pas suivi (re-mesure du 2026-08-22, story 67.4, contre la
            # preprod). Il disait : << cette table porte `published` et rien
            # d'autre (139 lignes, 0 pour les trois autres), donc trois des
            # quatre positions sont grisees sur TOUT flux de ce produit >>. La
            # table porte aujourd'hui 95 lignes et LES QUATRE etages :
            # collected 28, mapped 28, processed 28, published 11. Un lecteur
            # qui croyait ce paragraphe pensait cette logique morte alors
            # qu'elle repond, et serait alle chercher ailleurs pourquoi un
            # etage est grise.
            #
            # Le CODE, lui, n'a jamais dependu du chiffre : `observed` est
            # derive des lignes, pas d'une liste. C'est le commentaire qui
            # affirmait, et c'est lui qui a vieilli.
            "availability_reason": {
                stage: (
                    None
                    if stage in observed
                    else (
                        f"No {stage} evidence was persisted for an exact execution of "
                        "this Datastream, so there is nothing to filter this table to."
                    )
                )
                for stage in ("collected", "mapped", "processed", "published")
            },
            "sample_state": "reachable" if connector else "unavailable",
            # ONE SENTENCE, AND IT NAMES NO STORY -- finding D-7 of the visual
            # review #69, ratified in the `Data` cell of
            # `datastream-workbench-and-wizard.md`.
            #
            # What was written here before said the mart was "keyed by provider"
            # and named the consolidated slice it holds: a cause, in the words of
            # the warehouse, on a screen where nobody can act on it. The sandbox
            # went one worse and rendered a whole panel naming story 47.5. The
            # ratified form is two clauses and no third: what cannot be done on
            # this tab, and where the reading that DOES exist is found instead.
            #
            # There is nothing to say when a sample CAN be drawn -- the console
            # renders the sample itself and never this key -- so the reachable
            # case carries no sentence rather than a deployment state.
            "sample_reason": (
                None
                if connector
                else (
                    "No sample and no export can be drawn here — this Datastream"
                    " receives a file, and the rows it received are read in the"
                    " last file that arrived, above."
                )
                if mode == "managed_feed"
                else (
                    "No sample and no export can be drawn here — this Datastream"
                    " reads a relation it does not own, and its columns are read"
                    " on Mapping."
                )
                if mode == "external_bq"
                else (
                    "No sample and no export can be drawn here — this Datastream"
                    " names no source to read from, and the day-by-day reading"
                    " above is the only reading it has."
                )
            ),
            "state": "available" if stages else "unavailable",
            # WHAT THIS DATASTREAM'S FILES BECOME, when they become a media plan
            # (ratified 2026-08-24). `None` on every Datastream that is not a
            # carrier, and the console draws nothing at all for it: the plan
            # cycle belongs beside the file cycle, on the tab that already reads
            # the last file that arrived, and nowhere else in the console.
            "media_plan": _media_plan_evidence(conn, project_id, datastream_id, base),
        }
        # WHY THE LATE-DIMENSION MEASUREMENT IS SERVED HERE TOO (2026-08-31).
        #
        # Amendment 14 of the 2026-08-11 review ends on a sentence this tab was
        # never given: « surtout rendre un graphique creux sur l'historique sans
        # une phrase qui dit pourquoi il est creux. Un trou expliqué est une
        # donnée ; un trou muet est un bug que l'opérateur impute au produit. »
        #
        # `dimension_history` was published on `processing` alone -- where the
        # change is COMPOSED. The hollow is not there. It is on the day grid and
        # in the reading of one day, which live on this tab, and which drew a
        # dimension empty over every day older than its first declaration with
        # nothing on the screen saying that was why.
        #
        # THE SAME MEASUREMENT, NEVER A SECOND ONE. One reader
        # (`datastream_dimension_history`), one payload shape, one set of words:
        # a note derived here and a note derived there would be two answers to
        # one question, and the day they disagreed the screen would be the last
        # to know. The provider bound is read the same way as on `processing`
        # too -- from the connector's own manifest -- so the two tabs cannot
        # publish two different reaches for the same re-collection.
        from core.datastream_dimension_history import (  # noqa: PLC0415
            read_dimension_history,
        )
        from core.datastream_source_catalogue import (  # noqa: PLC0415
            read_source_catalogue,
        )

        _data_catalogue = read_source_catalogue(
            source_kind=base.get("source_kind"),
            module_name=base.get("module_name"),
            report_id=None,
        )
        evidence["dimension_history"] = read_dimension_history(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            source_kind=base.get("source_kind"),
            max_provider_backfill_days=_data_catalogue.get("max_provider_backfill_days"),
        )
    elif tab == "mapping":
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,version_number,content_hash,source_schema_hash,capability_fingerprint,
                          plan_version_id,executable,blocking_count,mapping_payload,ossie_projection,
                          created_by,created_at
                     FROM app.datastream_mapping_versions
                    WHERE project_id=%s AND datastream_id=%s ORDER BY version_number DESC""",
                (project_id, datastream_id),
            )
            evidence = {
                "versions": _rows(cur),
                "active_version": base.get("current_mapping_version_id"),
            }
        # Story 60.6: what each source column BECOMES, one row per column, the
        # excluded ones included -- "leur absence serait l'inventaire perdu".
        #
        # Computed on the SERVER, for every version in the list, because the word
        # is a vocabulary and a second writer of it in the console is exactly the
        # pair of copies free to diverge that story 59.5 spent a day repairing.
        # Everything it needs is already in memory: the payload carries the
        # bindings, the declared joins/splits and the profiled sample values.
        _attach_column_treatments(conn, project_id, datastream_id, evidence["versions"])
        # Story 27.8: the report each version pulls, so the language-binding
        # gesture on this tab can name it instead of asking for it.
        _attach_source_reports(conn, project_id, datastream_id, evidence["versions"])
        #  Ce que ce mapping POURRAIT nommer comme identite partagee, et ce
        #  que cela debloque. Sans cette lecture, la personne ouvre un
        #  selecteur par colonne, sur chaque flux, et ne voit jamais l effet.
        evidence["identity_candidates"] = _identity_candidates(
            conn, project_id, datastream_id, evidence["versions"], evidence["active_version"]
        )
        #  Story 71.3: le grain candidat par metrique bindee -- la mesure rapportee
        #  contre les dimensions bindees a son grain. Derive, jamais ecrit ; le
        #  geste « confirmer le grain » le declare comme version gouvernee VIA le
        #  MDM. Sans cette lecture, une personne ne verrait jamais quel grain ses
        #  colonnes deja liees dessinent.
        evidence["measurement_grain_candidates"] = _measurement_grain_candidates(
            conn, project_id, datastream_id, evidence["versions"], evidence["active_version"]
        )
        # NO CAPABILITY PROJECTION ON THIS TAB -- amendment 3 of the 2026-08-11
        # review. It used to answer "what does this capability do to my schema and
        # grain" here, and `Overview` answered the same question a tab away. The
        # panel was unmounted from the console the same day; this read stayed, and
        # a read nobody renders is not free -- `_latest_proposals` opens
        # `app.datastream_capability_proposals` on every load of this tab.
    elif tab == "processing":
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,version_number,normalized_payload,content_hash,
                          executable,validation_issues,
                          created_by,created_at FROM app.datastream_plan_versions
                    WHERE project_id=%s AND datastream_id=%s ORDER BY version_number DESC""",
                (project_id, datastream_id),
            )
            config = _json(base.get("config"), {})
            dest = config.get("destination") or {}
            evidence = {
                "plans": _rows(cur),
                "active_version": base.get("current_plan_version_id"),
                # Step 3 of the ordered chain is "map to governed fields", and it
                # is pinned to a mapping version, not a plan version. Naming it
                # here keeps the chain readable without a second request.
                "active_mapping_version": base.get("current_mapping_version_id"),
                "raw_zone_policy": dest.get("policy", "managed_raw"),
                "retention_days": dest.get("retention_days"),
            }
        # Story 60.3: the cleanup rules that reach this Datastream, READ-ONLY.
        # `datastream-workbench-and-wizard.md:989` says governed rules are
        # referenced here and not edited, so this key carries no identifier a
        # control could write to -- it carries what each rule does, in words.
        # A read that FAILS says so; it never renders as "no cleanup rule".
        evidence["cleanup_rules"] = _cleanup_rule_chain(conn, project_id, datastream_id)
        # AMENDMENT 13 (2026-08-11): what the connector declares as PULLABLE, so
        # a dimension can be added from a screen instead of the raw contract
        # editor. Read from the module's own manifest through the projection the
        # creation wizard already uses -- one catalogue, not two.
        #
        # The report is read from the plan the selector edits, active when there
        # is one and the HEAD when there is not: 4 of the 6 live Datastreams have
        # never published, and reading `current_plan_version_id` alone would hand
        # every one of them an empty catalogue.
        from core.datastream_source_catalogue import read_source_catalogue  # noqa: PLC0415

        _plans = evidence["plans"]
        _base_plan = next(
            (row for row in _plans if row.get("id") == base.get("current_plan_version_id")),
            _plans[0] if _plans else None,
        )
        _plan_source = (_json((_base_plan or {}).get("normalized_payload"), {}) or {}).get("source")
        _plan_source = _plan_source if isinstance(_plan_source, dict) else {}
        evidence["source_catalogue"] = read_source_catalogue(
            source_kind=base.get("source_kind"),
            # The plan's own `source.module` is what `app.datastreams.module_name`
            # was written from (`datastream_intents.py:778`); reading the plan
            # keeps the catalogue bound to the version the selector amends.
            module_name=_plan_source.get("module") or base.get("module_name"),
            report_id=_plan_source.get("report_id"),
        )
        # WHAT THOSE COLLECTION-WRITTEN COLUMNS ACTUALLY HOLD ON THIS PROJECT'S
        # ROWS (2026-08-12). The catalogue above DECLARES that every landed row
        # carries its collection instant; this READS it, from `fact_daily_kpi`
        # where the row is, so the screen shows a value before it explains one.
        # Kept apart from `read_source_catalogue` because it is a warehouse round
        # trip that can fail on its own -- and a warehouse that cannot be read must
        # not take the declaration down with it.
        from core.datastream_collection_provenance import (  # noqa: PLC0415
            read_collection_provenance,
        )

        evidence["source_catalogue"]["collection_provenance"] = read_collection_provenance(
            project_id=project_id,
            # The connector as the MART knows it -- `module_name`, the same column
            # the `cost` tab keys on. `config.connector_name` is a display name and
            # keying on it attributes a reading to the wrong flux.
            connector=str(base.get("module_name") or ""),
            source_kind=base.get("source_kind"),
        )
        # AMENDMENT 14 (2026-08-11): « Ajouter une dimension déclare une DETTE
        # D'HISTORIQUE, et l'écran la nomme ». What each day of the bounded window
        # was ASKED to carry, resolved through the run that collected it and never
        # through the plan in force today. Three statements, whatever the width of
        # the window and whatever the number of declared dimensions -- see the
        # module for why the per-dimension count is derived on the screen and not
        # published one day-list per field.
        from core.datastream_dimension_history import (  # noqa: PLC0415
            read_dimension_history,
        )

        evidence["dimension_history"] = read_dimension_history(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            source_kind=base.get("source_kind"),
            # The bound the catalogue just read from the manifest, passed rather
            # than read a second time: one reading of one manifest per load.
            max_provider_backfill_days=evidence["source_catalogue"].get(
                "max_provider_backfill_days"
            ),
        )
        # NO CAPABILITY PROJECTION ON THIS TAB EITHER, and this one cost the most:
        # eight impact blocks compiled per load, for a panel the console stopped
        # mounting on 2026-08-11. Same amendment 3, same reason -- `Overview` owns
        # the projection because its subject IS the posture.
    elif tab == "cost":
        # THE TAB THE CAPABILITY OPENS. Its own module, because the capability
        # state decides -- alone, before anything else and in one statement --
        # whether the warehouse is asked at all, and that decision is a read model
        # of its own rather than a branch inside a dispatcher.
        from core.datastream_workbench_cost import read_cost_evidence  # noqa: PLC0415

        evidence = read_cost_evidence(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            # The connector as the MART knows it. `identity.connector` on the
            # header prefers `config.connector_name`, which is a display name; the
            # ladder and the ambiguity rule both key on `module_name`, and reading
            # two different columns for one word is how a slice gets attributed to
            # the wrong flux.
            connector=str(base.get("module_name") or ""),
        )
    elif tab == "placements":
        # THE SECOND TAB A CAPABILITY OPENS -- story 61.1, same shape as `cost`
        # one branch above: its own read model, because the capability state
        # decides alone and first whether a plan and a warehouse are read at all.
        from core.datastream_workbench_placements import (  # noqa: PLC0415
            read_placements_evidence,
        )

        evidence = read_placements_evidence(
            conn,
            project_id=project_id,
            datastream_id=datastream_id,
            # `module_name`, for the same reason `cost` reads it: it is what
            # `fact_daily_kpi.connector` and `app.plan_line_mappings.connector`
            # carry. `identity.connector` prefers `config.connector_name`, a
            # display name, and matching a plan on a display name would attach a
            # campaign to the wrong flux.
            connector=str(base.get("module_name") or ""),
            # WHICH PLAN, arbitrage A5. It comes from the query string because no
            # Project designates an active plan -- only a VERSION carries
            # `is_active` -- so the choice belongs to the person reading, and the
            # payload names both the plan read and every plan it could have read.
            plan_id=(options or {}).get("plan_id"),
        )
    elif tab == "runs":
        # AMENDED 2026-08-18: the list is PAGED and NARROWED by the server.
        #
        # What it was: 200 rows, newest first, and a sentence saying there were
        # more. Nothing could walk past that cap -- « this Datastream has more »
        # was the whole of the answer -- and the state chips filtered the 200
        # rows already in hand, so a chip over a capped list narrowed a sample
        # and called it a history.
        #
        # PAGED BY KEYSET ON `id DESC`, the pattern of `query_specs.
        # list_query_specs` and `analyze_artifacts.list_renders`. The id is
        # `dse_` + a ULID (migration 042 CHECKs it), so `id DESC` IS creation
        # order, and a page boundary cannot skip or repeat a run when one is
        # minted mid-scan -- which an OFFSET would, on the one table that grows a
        # row every night at the top.
        filters = _run_filters(options)
        where, params = _run_where(project_id, datastream_id, filters)
        cursor = filters.get("cursor")
        page_where, page_params = where, list(params)
        if cursor:
            page_where += " AND id < %s"
            page_params.append(cursor)
        with conn.cursor() as cur:
            cur.execute(
                # Story 63.1: same columns on every run of the list, so the tab
                # can show a run that is still moving instead of only runs that
                # have stopped.
                f"""SELECT id,state,state_changed_at,created_at,updated_at,plan_version_id,
                          mapping_version_id,projection_plan_ref,row_count,content_hash,
                          error_code,error_detail,artifact_ref,adapter_ref,
                          step,day_in_progress,days_done,days_total,rows_written,
                          started_at,progress_updated_at
                     FROM app.datastream_executions WHERE {page_where}
                    ORDER BY id DESC LIMIT %s""",
                (*page_params, RUN_LIST_LIMIT + 1),
            )
            runs = _rows(cur)
            # One row over the page is how "is there a next page" is answered
            # without a second count: it is read and dropped, never rendered.
            has_more = len(runs) > RUN_LIST_LIMIT
            runs = runs[:RUN_LIST_LIMIT]
            next_cursor = str(runs[-1]["id"]) if has_more and runs else None
            # HOW MANY MATCH, over the whole collection and not over the page.
            # It is what turns "showing the 200 most recent" into "200 of 428":
            # the first is a cap a reader can only distrust, the second is a
            # measurement they can act on.
            cur.execute(
                f"SELECT COUNT(*) FROM app.datastream_executions WHERE {where}",
                tuple(params),
            )
            matching = int(cur.fetchone()[0])
            # THE CHIPS ARE READ FROM THE COLLECTION, NEVER FROM THE PAGE. A chip
            # list derived from 200 rows would gain and lose entries as somebody
            # pages, so a state present on page three would have no chip until
            # they got there -- and clearing the chip would be the only way back.
            # Unfiltered on purpose: these are the values that EXIST, which is a
            # different question from the ones the current narrowing left.
            cur.execute(
                """SELECT DISTINCT state FROM app.datastream_executions
                    WHERE project_id=%s AND datastream_id=%s""",
                (project_id, datastream_id),
            )
            present_states = sorted(str(row[0]) for row in cur.fetchall() if row[0])
            cur.execute(
                """SELECT DISTINCT projection_plan_ref ->> 'origin'
                     FROM app.datastream_executions
                    WHERE project_id=%s AND datastream_id=%s
                      AND projection_plan_ref ->> 'origin' IS NOT NULL""",
                (project_id, datastream_id),
            )
            present_origins = sorted(str(row[0]) for row in cur.fetchall() if row[0])
            # Two reads for the whole list, not two per run: a Datastream that
            # ran every night for a year is 200 rows on this tab, and a query
            # inside the loop would be 400 round trips for two counters.
            spans = _step_evidence(conn, project_id, datastream_id)
            anomalies = _run_anomalies(conn, project_id, datastream_id)
            for run in runs:
                projection = _json(run.pop("projection_plan_ref", None), {})
                # Story 63.7: WHY this run exists, as ONE derived field. The
                # plan itself still does not leave the server -- it carries the
                # compiled projection and the recovery scope -- but the tab
                # showed a list of treatments with no way to tell a nightly
                # collection from a mapping change, and an unaccounted-for run
                # reads as an anomaly. `None` for every run minted before this
                # story, which the column says rather than guesses.
                run["origin"] = origin_of(projection)
                interval = projection.get("half_open_range") or projection.get("interval")
                run["recovery"] = _recovery_options(
                    conn,
                    base=base,
                    run=run,
                    interval=interval if isinstance(interval, dict) else None,
                )
                # Story 58.10: the run's OWN reading -- how long it took, how
                # long each of its four steps took, and how many anomalies were
                # found on it. Absent everywhere it was not measured; a `0`
                # would be a measurement none of these runs made.
                run["duration_seconds"] = _run_duration_seconds(run)
                # The span alone cannot say whether a step is still working;
                # the run's state can, and four writers reach a terminal state
                # without closing anything.
                run["steps"] = _mark_step_spans(
                    spans.get(str(run.get("id")), []), run.get("state")
                )
                run["anomalies"] = anomalies["runs"].get(
                    str(run.get("id")), {"anomalies": 0, "evaluations": 0, "issues": []}
                )
            evidence = {
                "runs": runs,
                # AT THE GRAIN OF THE FLUX, and rendered once -- story 59.1,
                # arbitrage 7. An evaluation with no `execution_id` belongs to no
                # run, so "no monitor evaluated on this run" stays true of every
                # run; what a person must not conclude from it is "the quality
                # was never checked". This is the count that answers that, and it
                # lives outside the run list because that is where its subject is.
                "evaluations_without_run": anomalies["evaluations_without_run"],
                # THE FOUR STEPS TRAVEL WITH THE PAYLOAD, so the console keeps no
                # second list of them. A hand-written vocabulary on one side is
                # how `collected` came to be painted "still waiting" for a whole
                # story (63.1); the same rule as `EXECUTION_STATES`.
                "step_vocabulary": list(PROGRESS_STEPS),
                # The cap is ON THE WIRE. A list silently truncated at 200 reads
                # as "this Datastream has run 200 times", which is a different
                # and false statement.
                "runs_limit": RUN_LIST_LIMIT,
                "runs_truncated": has_more,
                # WHERE THE REST OF THE COLLECTION IS. `runs_truncated` said
                # there was more and gave nobody a way to it; this is the way,
                # and the cap sentence stops being a dead end.
                "runs_next_cursor": next_cursor,
                "runs_cursor": cursor,
                "runs_matching": matching,
                # What was actually applied, echoed so the screen states the
                # narrowing it is showing rather than trusting its own state --
                # the two disagree the moment a re-read fails halfway.
                "runs_filters": filters,
                # The values that EXIST on this Datastream. The chips are drawn
                # from these, so they neither offer a state no run carries nor
                # lose one because the current page happens not to hold it.
                "run_states_present": present_states,
                "run_origins_present": present_origins,
                "timeline": _phase_evidence(conn, project_id, datastream_id),
            }
    elif tab == "outputs":
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.id,o.output_kind,o.stable_name,v.id AS version_id,v.execution_id,
                          v.publication_log_id,v.plan_version_id,v.mapping_version_id,
                          v.projection_version_ref,v.relation_ref,v.delivery_ref,v.schema_hash,
                          v.grain_evidence,v.evidence,v.created_at
                     FROM app.datastream_outputs o LEFT JOIN app.datastream_output_versions v
                       ON v.output_id=o.id
                    WHERE o.project_id=%s AND o.datastream_id=%s
                    ORDER BY o.output_kind,o.stable_name,v.created_at DESC""",
                (project_id, datastream_id),
            )
            config = _json(base.get("config"), {})
            dest = config.get("destination") or {}
            evidence = {
                "outputs": _rows(cur),
                "pointers": compose_header(base)["publications"],
                "raw_zone_policy": dest.get("policy", "managed_raw"),
                "retention_days": dest.get("retention_days"),
            }
            cur.execute(
                """SELECT u.output_id,u.output_version_id,u.consumer_kind,u.consumer_ref,
                          u.consumer_version_ref,u.owner_href,u.created_at
                     FROM app.datastream_output_used_by u
                     JOIN app.datastream_outputs o ON o.id=u.output_id
                    WHERE o.project_id=%s AND o.datastream_id=%s
                    ORDER BY u.consumer_kind,u.consumer_ref,u.output_version_id""",
                (project_id, datastream_id),
            )
            evidence["used_by"] = _rows(cur)
    else:
        raise WorkbenchValidationError("Unknown Datastream Workbench tab")
    return compose_tab_payload(
        tab, datastream_id=datastream_id, project_id=project_id, evidence=evidence
    )


# ---------------------------------------------------------------------------
# THE `Runs` NARROWING -- amended 2026-08-18.
#
# AT THE TAIL OF THE MODULE ON PURPOSE, and not beside `RUN_LIST_LIMIT` where
# the subject belongs. Sixteen files across this repository cite a `path:line`
# into this module -- the completeness ledger, two ratified documents, the screen
# board, four suites and three components -- and inserting eighty lines near the
# top moves every one of them by eighty. `read_tab` resolves these names at call
# time, so the placement costs nothing and keeps the citation graph true.
# ---------------------------------------------------------------------------

#: The narrowings the `Runs` tab may ask this route for.
#:
#: WHY THEY MOVED TO THE SERVER, AND WHY THE 2026-08-07 SENTENCE IS WITHDRAWN.
#: `datastream-workbench-and-wizard.md` said « le filtre d'état est côté client
#: ... sans `?state=` sur une route partagée ». That held while the tab drew one
#: uncapped list; it stopped holding the moment the list became PAGED. A chip
#: applied to the page a person is standing on answers "no run in this state"
#: while the collection holds forty of them -- a filter that lies about the
#: collection is worse than no filter, and it is exactly what the cap sentence at
#: the top of the tab already warned about.
#:
#: THE ADDRESS IS NOT SHARED IN THE SENSE THAT SENTENCE MEANT. `read_tab` takes
#: `options` per tab and has since story 61.1 -- `placements` reads `plan_id` off
#: the same query string, and no other tab sees it. A key a single branch reads
#: is a parameter of that branch, not a tab's need written into everybody's
#: address.
RUN_FILTER_KEYS: tuple[str, ...] = ("state", "origin", "from", "to", "q", "cursor")

#: The longest free-text search accepted. A bound rather than none, for the same
#: reason every other free-text column in this repository carries one.
RUN_SEARCH_MAX = 120


def _run_filters(options: dict[str, Any] | None) -> dict[str, str]:
    """The narrowings a caller asked for, trimmed, with the empties dropped.

    An absent key and a key set to the empty string are the SAME request here --
    "do not narrow on this" -- because a console clearing a text field sends the
    second and means the first. Everything else is passed to the SQL below as a
    parameter, never interpolated.
    """
    asked = options or {}
    filters: dict[str, str] = {}
    for key in RUN_FILTER_KEYS:
        value = str(asked.get(key) or "").strip()
        if not value:
            continue
        filters[key] = value[:RUN_SEARCH_MAX] if key == "q" else value
    return filters


def _run_where(project_id: str, datastream_id: str, filters: dict[str, str]) -> tuple[str, list]:
    """The WHERE clause and its parameters, for the page AND for the counts.

    ONE COMPOSER FOR BOTH READS. The count under the table and the rows in it are
    two statements over one predicate; two spellings of that predicate is how a
    screen comes to say "12 runs match" above eleven rows.

    A date is compared as a DATE and the upper bound is INCLUSIVE of its day:
    somebody who asks for `to = 2026-08-17` means the whole of the 17th, and
    `created_at < '2026-08-17'` would silently drop every run of it.
    """
    where = "project_id = %s AND datastream_id = %s"
    params: list[Any] = [project_id, datastream_id]
    if state := filters.get("state"):
        where += " AND lower(state) = lower(%s)"
        params.append(state)
    if origin := filters.get("origin"):
        where += " AND projection_plan_ref ->> 'origin' = %s"
        params.append(origin)
    if start := filters.get("from"):
        where += " AND created_at >= %s::date"
        params.append(start)
    if end := filters.get("to"):
        where += " AND created_at < (%s::date + 1)"
        params.append(end)
    if search := filters.get("q"):
        # THE TWO STRINGS A PERSON ARRIVES WITH. They come to this tab holding a
        # run id out of a ticket, or an error code out of an alert; nothing else
        # on the row is a handle somebody could have been given elsewhere.
        where += " AND (id ILIKE %s OR error_code ILIKE %s)"
        like = f"%{search}%"
        params.extend([like, like])
    return where, params
