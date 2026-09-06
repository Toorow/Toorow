"""Shared owner-backed Project readiness projection for Overview and Getting Started.

ONE EVIDENCE, ONE DERIVATION, TWO READERS. `overview.md:135` states it -- *"Overview
and Getting Started use the same authoritative Project, readiness and responsibility
objects"* -- and until the audit of 2026-08-17 only the COMPOSER was shared. The
evidence was not: Getting Started selected the oldest ENABLED Datastream and the most
recent row of `app.datastream_publication_log`, while Overview handed the composer
`rows[0]` of its own evidence query -- the first Datastream BY NAME, disabled ones
included -- and that row's `current_published_execution_id`. A Project with two
Datastreams could therefore read `first_value: ready` on one screen and `blocked` on
the other, about the same Project, in the same second.

So the SELECTION moved in here too. `select_project_readiness_evidence` is the single
query path; `compose_project_readiness` is selection + composition, and it is what both
surfaces call. `compose_project_readiness_from_evidence` stays public for the callers
that already hold the evidence (tests, and any future surface that has measured it).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: v2 adds the `governance` component. v1 had four, and the Getting Started journey
#: carried a fifth STEP that no component could ever judge -- so its progress was
#: capped at 4/5 for every Project, forever.
READINESS_SCHEMA_VERSION = "project-readiness.v2"

#: Every component of the projection, in journey order. A reader that renders the
#: projection iterates THIS, so a sixth component reaches both surfaces at once.
READINESS_COMPONENTS = (
    "project_foundation",
    "source",
    "datastream",
    "governance",
    "first_value",
)

#: How far back the governance evidence looks, matching the DQ report's own window
#: (`dq_api.fetch_dq_report_data`) so the two never disagree about the same firings.
_GOVERNANCE_WINDOW_DAYS = 30


def _owner(
    workspace: str,
    section: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    return {
        "surface": "project",
        "workspace": workspace,
        "section": section,
        "global_surface": None,
        "global_section": None,
        "object_type": object_type,
        "object_id": object_id,
        "tab": None,
        "action": action,
        "version_id": None,
        "evidence_id": None,
    }


def _global(surface: str, section: str) -> dict[str, Any]:
    return {
        "surface": "global",
        "workspace": None,
        "section": None,
        "global_surface": surface,
        "global_section": section,
        "object_type": None,
        "object_id": None,
        "tab": None,
        "action": None,
        "version_id": None,
        "evidence_id": None,
    }


def compose_project_readiness_from_evidence(
    *,
    active_configuration_version_id: str | None,
    source_id: str | None,
    datastream_id: str | None,
    first_value_id: str | None,
    governance_evidence_date: str | None = None,
    governance_unresolved_issues: int = 0,
) -> dict[str, Any]:
    # GOVERNANCE IS READY WHEN GOVERNANCE HAS LOOKED AND FOUND NOTHING BLOCKING.
    # Two conditions, and the first is the one that is easy to forget: an absence
    # of unresolved quality issues means nothing on a Project where no monitor has
    # ever evaluated anything. Zero out of zero is not an all-clear
    # (`overview.md:123` -- "Every count names its denominator and window"), and
    # `overview.md:118` -- "Unknown is not healthy". So the component asks for a
    # DAY on which the monitors could run, and only then for silence.
    governance_ready = bool(governance_evidence_date) and not governance_unresolved_issues
    components = {
        "project_foundation": {
            "state": "ready" if active_configuration_version_id else "blocked",
            "owner": _global("project-settings", "general"),
            "evidence_ref": active_configuration_version_id,
        },
        "source": {
            "state": "ready" if source_id else "blocked",
            "owner": _owner("data", "sources"),
            "evidence_ref": source_id,
        },
        "datastream": {
            "state": "ready" if datastream_id else "blocked",
            # With no Datastream yet the owner is the COLLECTION and its `create`
            # action. Naming an object id of "new" would assert an object that
            # does not exist, in a route model whose identifiers are opaque.
            "owner": (
                _owner("data", "datastreams", object_type="datastream", object_id=datastream_id)
                if datastream_id
                else _owner("data", "datastreams", action="create")
            ),
            "evidence_ref": datastream_id,
        },
        "governance": {
            # WHY THIS COMPONENT EXISTS. The Getting Started journey has always
            # carried a "Review Governance readiness" step, and no readiness
            # component owned it. `getting_started._STEP_READINESS` skipped it, the
            # step stayed on its persisted state forever, and the progress bar of
            # EVERY Project was capped at 4 of 5. A step no evidence can ever
            # complete is not a step -- so either the step goes, or the evidence
            # arrives. The evidence exists: it is the same DQ firing window the
            # Governance workbench itself reads.
            "state": "ready" if governance_ready else "blocked",
            "owner": _owner("governance", "controls-quality"),
            "evidence_ref": governance_evidence_date if governance_ready else None,
        },
        "first_value": {
            "state": "ready" if first_value_id else "blocked",
            # WHERE THE FIRST PUBLICATION LANDS -- amendment of 2026-08-17 to
            # `first-figure-path.md`: "the first-publication gesture lands on THE
            # WEB SHARE AND THE MCP APP: publishing produces a shared render
            # reachable by web link (...). No new console destination is built
            # for it."
            #
            # This owner used to name the Datastream object with
            # `action="first-publication"`. The `datastream` navigation contract
            # declares no actions, so `ContentRouter.openOwner` refused it and
            # returned -- the single most important item of a new Project opened
            # NOTHING. The destination is now the Renders collection, which is
            # where a published render and its web share already live
            # (`analyze/renders`, tabs result / evidence / sharing).
            "owner": (
                _owner("analyze", "renders")
                if datastream_id
                # No Datastream yet: the gesture is still to create one, and the
                # collection declares that action.
                else _owner("data", "datastreams", action="create")
            ),
            "evidence_ref": first_value_id,
        },
    }
    encoded = json.dumps(components, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "version": hashlib.sha256(encoded.encode()).hexdigest()[:24],
        **components,
    }


def _select_governance_evidence(project_id: str, conn) -> tuple[str | None, int]:
    """The last day the DQ monitors could evaluate, and what is still unresolved.

    Same table, same window and same `dq_` family as `dq_api.fetch_dq_report_data`,
    so the journey step and the Governance workbench never disagree about the same
    firings. Its own transaction: a failure here must not abort the caller's, and a
    Project whose governance evidence cannot be read is `blocked`, never `ready` --
    an unreadable monitor is not a quiet one.
    """
    try:
        with conn.transaction():
            # "Still open" for a firing is `infra_alerts`' one sentence, consumed
            # rather than respelled -- the table has no `status` column, so a
            # hand-typed `acknowledged_at IS NULL` here is a second definition of
            # a word the console prints.
            from core.infra_alerts import firing_status_predicate  # noqa: PLC0415

            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                      (SELECT COUNT(*)
                         FROM app.alert_firings af
                        WHERE af.project_id = %s
                          AND af.type LIKE 'dq_%%'
                          AND {firing_status_predicate("open", alias="af")}
                          AND af.fired_at >= NOW() - make_interval(days => %s)),
                      (SELECT MAX(DATE(pj.completed_at))
                         FROM app.pull_jobs pj
                         JOIN app.datastreams ds ON ds.id = pj.datastream_id
                        WHERE ds.project_id = %s
                          AND pj.completed_at IS NOT NULL
                          AND pj.completed_at >= NOW() - make_interval(days => %s))
                    """,
                    (
                        project_id,
                        _GOVERNANCE_WINDOW_DAYS,
                        project_id,
                        _GOVERNANCE_WINDOW_DAYS,
                    ),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- degrade to blocked, never to ready
        logger.warning("readiness: governance_unavailable project=%s: %s", project_id, exc)
        return None, 0
    if not row:
        return None, 0
    evaluated_day = row[1]
    return (str(evaluated_day) if evaluated_day else None), int(row[0] or 0)


def select_project_readiness_evidence(project_id: str, conn) -> dict[str, Any]:
    """THE one query path behind the readiness projection.

    Overview used to run its own -- `rows[0]` of a fleet query ordered by NAME,
    disabled Datastreams included. Two surfaces promising the same object derived
    it from two evidences. There is now one place to read, and one place to fix.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT active_configuration_version_id FROM app.projects "
            "WHERE id=%s AND status='active'",
            (project_id,),
        )
        project = cur.fetchone()
        if not project:
            raise ValueError("Project not found")
        cur.execute(
            "SELECT id, connection_ref_id FROM app.datastreams "
            "WHERE project_id=%s AND enabled=TRUE ORDER BY created_at LIMIT 1",
            (project_id,),
        )
        datastream = cur.fetchone()
        # The source EVIDENCE is the authorization this Datastream actually
        # reads through. It used to be the project's oldest active credential,
        # picked independently of the Datastream just above it -- so a project
        # with two authorizations showed one as proof of a Datastream fed by the
        # other. Nothing tied the two rows together; they only agreed while a
        # project had exactly one credential.
        #
        # A project with no Datastream yet still has a source step to satisfy,
        # so the oldest active credential remains the answer for that case.
        source = (datastream[1],) if datastream and datastream[1] else None
        if source is None:
            cur.execute(
                "SELECT id FROM app.connection_ref "
                "WHERE project_id=%s AND status='active' ORDER BY created_at LIMIT 1",
                (project_id,),
            )
            source = cur.fetchone()
        if datastream:
            # NO `state` PREDICATE. `state` is a column of
            # `app.datastream_executions`, not of the publication log, and asking
            # for it here raised `UndefinedColumn: column "state" does not exist`
            # on EVERY call -- which is what answered 503 to every Getting Started
            # read in production. Named at last by the log line added to the route
            # on 2026-08-04; before that the handler discarded the exception.
            #
            # The log needs no predicate: `datastream_publication.py:1605` states
            # the rule -- *"the log, pointer and outbox are written in one
            # transaction, so the log is sufficient commit-evidence"*. A row IS a
            # publication that landed. The most recent one is the first value.
            cur.execute(
                "SELECT execution_id FROM app.datastream_publication_log "
                "WHERE datastream_id=%s ORDER BY published_at DESC LIMIT 1",
                (datastream[0],),
            )
            first_value = cur.fetchone()
        else:
            first_value = None
    governance_day, governance_unresolved = _select_governance_evidence(project_id, conn)
    return {
        "active_configuration_version_id": project[0],
        "source_id": source[0] if source else None,
        "datastream_id": datastream[0] if datastream else None,
        "first_value_id": first_value[0] if first_value else None,
        "governance_evidence_date": governance_day,
        "governance_unresolved_issues": governance_unresolved,
    }


def compose_project_readiness(project_id: str, conn) -> dict[str, Any]:
    """Select the evidence once, compose it once. Both surfaces call THIS."""
    return compose_project_readiness_from_evidence(
        **select_project_readiness_evidence(project_id, conn)
    )
