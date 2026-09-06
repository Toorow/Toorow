"""Authoritative Project Overview read model (Epic 43, Story 43.15).

This module composes only project-scoped governance evidence. It does not query
analytical facts or infer business metrics in the browser.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.project_readiness import READINESS_COMPONENTS, compose_project_readiness

logger = logging.getLogger(__name__)

# `authorize_overview_project` and `build_project_overview` STOOD HERE AND WERE
# DELETED (audit of 2026-08-17). Neither was reachable: the live route authorizes
# through `project_overview_api._strict_project_capability_allowed`, and the
# envelope is built by `compose_project_overview` below. `build_project_overview`
# had no caller at all; `authorize_overview_project` had exactly one, its own test
# -- so a green suite was proving an authorization path that no deployment ever
# walked, which is worse than no test.

PROJECT_OVERVIEW_SCHEMA_VERSION = "project-overview.v1"

#: Look-back for the governed alert firings shown as business signals, and how
#: many are carried. Seven days rather than the briefing's 24 hours because this
#: page is opened by a person on their own cadence, not sent every morning; the
#: window is stated on the zone's evidence horizon, never implied.
_ALERT_SIGNAL_WINDOW_HOURS = 168
_ALERT_SIGNAL_LIMIT = 5

#: Said on every alert item, whatever it opens: a firing is one observation against
#: one threshold, never the whole reading behind it. It used to end with "is not
#: reachable from this console yet", and that half was doing a link's job -- the
#: item named its limit and opened nothing (audit of 2026-08-17, gap 5). The
#: destination is composed by `_alert_signal_owner` now, so this sentence says only
#: what remains true of every firing.
_ALERT_SIGNAL_LIMITATION = (
    "One observation against one threshold, not the full reading behind it."
)

#: Said on a firing this console has NOTHING to open for -- and it names what is
#: missing rather than staying silent. `overview.md` (amendment of 2026-08-21):
#: pointing such a firing at a collection that does not contain it would be a
#: plausible destination, which is worse than none. Measured instance:
#: `dq_geography` is Project-scoped, so no `app.dq_monitors` row can hold it and
#: the Data Quality collection would open on a list without it.
_ALERT_SIGNAL_NO_DESTINATION = (
    "This alert names no Datastream, so there is nothing to open here; ask the "
    "assistant for the daily report to read the alert in full."
)

#: The firing type media-plan pacing writes (`mediaplan_alerts.ALERT_TYPE`).
#: Restated rather than imported: importing `mediaplan_alerts` pulls a writer and
#: its database session into a read model whose whole point is that it composes.
_MEDIAPLAN_PACE_ALERT_TYPE = "mediaplan_pace"
_OWNER_FIELDS = (
    "surface",
    "workspace",
    "section",
    "global_surface",
    "global_section",
    "object_type",
    "object_id",
    "tab",
    "action",
    "version_id",
    "evidence_id",
)


def owner_reference(
    workspace: str,
    section: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    tab: str | None = None,
    action: str | None = None,
    version_id: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Return semantic route data; browser URLs are built only by the client."""
    return {
        "surface": "project",
        "workspace": workspace,
        "section": section,
        "global_surface": None,
        "global_section": None,
        "object_type": object_type,
        "object_id": object_id,
        "tab": tab,
        "action": action,
        "version_id": version_id,
        "evidence_id": evidence_id,
    }


def _global_owner_reference(
    surface: str,
    section: str,
    *,
    evidence_id: str | None = None,
) -> dict[str, Any]:
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
        "evidence_id": evidence_id,
    }


def _validate_owner(owner: Any) -> dict[str, Any]:
    if not isinstance(owner, dict) or set(owner) != set(_OWNER_FIELDS):
        raise ValueError("owner reference is incomplete")
    if owner["surface"] not in {"project", "global"}:
        raise ValueError("owner surface is invalid")
    if owner["surface"] == "project" and not (owner["workspace"] and owner["section"]):
        raise ValueError("project owner is incomplete")
    if owner["surface"] == "global" and not (owner["global_surface"] and owner["global_section"]):
        raise ValueError("global owner is incomplete")
    return owner


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def rank_attention_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate one root cause and apply the ratified deterministic order."""
    grouped: dict[str, dict[str, Any]] = {}
    for candidate in items:
        key = str(candidate.get("root_cause_key") or candidate.get("id") or "")
        if not key:
            raise ValueError("attention item requires a stable root cause")
        owner = _validate_owner(candidate.get("owner"))
        priority = int(candidate.get("priority_class", 5))
        current = grouped.get(key)
        if current is None:
            grouped[key] = {
                **candidate,
                "id": str(candidate.get("id") or key),
                "root_cause_key": key,
                "priority_class": priority,
                "impact": sorted(set(_values(candidate.get("impact")))),
                "scope": sorted(set(_values(candidate.get("scope")))),
                "owner": owner,
            }
            continue
        current["priority_class"] = min(current["priority_class"], priority)
        current["impact"] = sorted(set(current["impact"] + _values(candidate.get("impact"))))
        current["scope"] = sorted(set(current["scope"] + _values(candidate.get("scope"))))
        first = [
            value
            for value in (
                current.get("first_observed_at"),
                candidate.get("first_observed_at"),
            )
            if value
        ]
        last = [
            value
            for value in (
                current.get("last_observed_at"),
                candidate.get("last_observed_at"),
            )
            if value
        ]
        current["first_observed_at"] = min(first) if first else None
        current["last_observed_at"] = max(last) if last else None
        if not (current.get("action") or {}).get("permitted") and (
            candidate.get("action") or {}
        ).get("permitted"):
            current["action"] = candidate["action"]
            current["owner"] = owner

    ranked = list(grouped.values())
    ranked.sort(
        key=lambda item: (str(item.get("last_observed_at") or ""), item["id"]), reverse=True
    )
    ranked.sort(
        key=lambda item: (
            item["priority_class"],
            -len(item["scope"]),
        )
    )
    return ranked


def _read_project_settings_projection(
    project_id: str,
    conn,
    *,
    can_edit: bool,
) -> dict[str, Any]:
    from core.project_settings import read_project_settings

    return read_project_settings(conn, project_id=project_id, can_edit=can_edit)


def _projection_state(status: str, state: str, explanation: str, owner: dict) -> dict:
    return {
        "status": status,
        "state": state,
        "explanation": explanation,
        "evidence_horizon": None,
        "owner": owner,
    }


def _query_governance_projection(project_id: str, conn) -> dict[str, Any]:
    owner = owner_reference("governance", "controls-quality")
    try:
        from core.dq_api import fetch_dq_report_data

        with conn.transaction():
            report = fetch_dq_report_data(project_id, conn)
    except Exception as exc:
        logger.warning("overview: governance_unavailable project=%s: %s", project_id, exc)
        return {
            **_projection_state(
                "unavailable",
                "unknown",
                "Governance evidence could not be loaded.",
                owner,
            ),
            "denominator": 0,
            "complete": 0,
            "gaps": ["Governance evidence unavailable"],
        }
    unresolved = int(report.get("total_unresolved") or 0)
    unavailable = bool(report.get("monitors_unavailable"))
    return {
        **_projection_state(
            "unavailable" if unavailable else "ready",
            "unknown" if unavailable else "degraded" if unresolved else "ready",
            (
                "Governance monitors are unavailable."
                if unavailable
                else f"{unresolved} unresolved quality issue(s)."
                if unresolved
                else "No unresolved quality issue is supported by current evidence."
            ),
            owner,
        ),
        "denominator": max(unresolved, 1) if not unavailable else 0,
        "complete": 0 if unresolved or unavailable else 1,
        "gaps": [f"{unresolved} unresolved quality issue(s)"] if unresolved else [],
    }


def _query_context_projection(project_id: str, conn) -> dict[str, Any]:
    owner = owner_reference("context-hub", "knowledge-graph")
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM app.context_topics
                       WHERE project_id = %s AND status = 'active'),
                      -- `app.procedures` (migration 196), NOT `app.context_procedures`:
                      -- that relation has never existed. The except-clause below
                      -- swallowed the UndefinedTable, so the Context projection read
                      -- "Context evidence unavailable" on every project forever --
                      -- an honest-looking empty state produced by a typo, which is
                      -- worse than a 500 because nothing ever pointed at it.
                      (SELECT COUNT(*) FROM app.procedures
                       WHERE project_id = %s AND status = 'active')
                    """,
                    (project_id, project_id),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.warning("overview: context_unavailable project=%s: %s", project_id, exc)
        return {
            **_projection_state(
                "unavailable",
                "unknown",
                "Context evidence could not be loaded.",
                owner,
            ),
            "denominator": 0,
            "complete": 0,
            "gaps": ["Context evidence unavailable"],
        }
    count = int(row[0] or 0) + int(row[1] or 0) if row else 0
    return {
        **_projection_state(
            "ready" if count else "empty",
            "ready" if count else "unknown",
            (
                f"{count} governed context object(s) are active."
                if count
                else "No governed context evidence is active."
            ),
            owner,
        ),
        "denominator": count,
        "complete": count,
        "gaps": [] if count else ["No governed context evidence"],
    }


def _query_test_projection(project_id: str, conn) -> dict[str, Any]:
    owner = owner_reference("test", "regression-runs")
    latest = _query_latest_test(project_id, conn)
    value = latest.get("value")
    if latest["status"] != "ready" or not value:
        return {
            **_projection_state(
                latest["status"],
                "unknown",
                "No verifiable Evaluation Run is available.",
                owner,
            ),
            "denominator": 0,
            "complete": 0,
            "gaps": ["Evaluation evidence unavailable"],
        }
    regressions = int(value["regressions"])
    return {
        "status": "ready",
        "state": "degraded" if regressions else "ready",
        "explanation": (
            f"{regressions} regression(s) require review."
            if regressions
            else "The latest Evaluation Run has no regression."
        ),
        "evidence_horizon": value.get("run_at"),
        "owner": owner,
        "denominator": int(value["total"]),
        "complete": int(value["passed"]),
        "gaps": [f"{regressions} regression(s)"] if regressions else [],
    }


def _query_recent_changes(project_id: str, conn) -> dict[str, Any]:
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, state, activated_version_id, confirmed_at
                    FROM app.project_change_sets
                    WHERE project_id = %s
                      AND state IN ('confirmed', 'failed', 'outcome_unknown')
                    ORDER BY confirmed_at DESC NULLS LAST, created_at DESC
                    LIMIT 8
                    """,
                    (project_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("overview: changes_unavailable project=%s: %s", project_id, exc)
        return {"status": "unavailable", "items": []}
    items = [
        {
            "id": str(row[0]),
            "kind": "configuration_change",
            "state": str(row[1]),
            "occurred_at": _iso_value(row[3]),
            "summary": "Project configuration changed.",
            "owner": _global_owner_reference(
                "project-settings",
                "changes",
                evidence_id=str(row[0]),
            ),
            "version_id": str(row[2]) if row[2] else None,
        }
        for row in rows
    ]
    return {"status": "ready" if items else "empty", "items": items}


def _legacy_attention_owner(item: dict[str, Any]) -> dict[str, Any]:
    """Name the object that OWNS one operational attention item.

    THE FIRST PUBLICATION IS NOT A DATASTREAM TAB. This function emitted
    `object_type="datastream"` with `action="first-publication"` for the
    `evidence_missing` item -- the single most important item of a new Project.
    The `datastream` navigation contract declares no actions
    (`ui/admin/src/shell/navigation/data.ts`), so `ContentRouter.openOwner`
    refused the reference and returned: "Open owner" clicked and did nothing.

    The amendment of 2026-08-17 to `first-figure-path.md` ratifies where the
    gesture lands -- "THE WEB SHARE AND THE MCP APP: publishing produces a
    shared render reachable by web link (...). No new console destination is
    built for it." So the owner is the Renders collection, the console surface
    that already holds a published render and its web share.
    """
    if item["target"] == "first-publication":
        return owner_reference("analyze", "renders")
    return owner_reference(
        "data",
        "datastreams",
        object_type="datastream",
        object_id=item["datastream_id"],
        tab=item["target"] if item["target"] in {"overview", "runs"} else None,
    )


def _operational_attention(
    legacy_items: list[dict[str, Any]],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    priority_by_reason = {
        "latest_run_failed": 1,
        "connection_unusable": 2,
        "verification_incomplete": 1,
        "coverage_incomplete": 1,
        "evidence_missing": 4,
    }
    return [
        {
            "id": f"data:{item['datastream_id']}:{item['reason']}",
            "root_cause_key": f"data:{item['datastream_id']}:{item['reason']}",
            "priority_class": priority_by_reason.get(item["reason"], 4),
            "cause": item["detail"],
            "impact": "Project data trust is limited.",
            "scope": [item["name"]],
            "status": "blocked" if item["reason"] != "evidence_missing" else "unknown",
            "first_observed_at": None,
            "last_observed_at": as_of,
            "evidence_horizon": None,
            "owner": _legacy_attention_owner(item),
            "action": {"label": "Open owner", "permitted": True},
        }
        for item in legacy_items
    ]


def _projection_attention(projection: dict[str, Any], kind: str) -> dict[str, Any] | None:
    if projection.get("state") in {"ready", "not_applicable"}:
        return None
    owner = _validate_owner(projection["owner"])
    return {
        "id": f"{kind}:{projection.get('state')}",
        "root_cause_key": f"{kind}:{projection.get('state')}",
        "priority_class": 4,
        "cause": projection.get("explanation") or "; ".join(projection.get("gaps") or []),
        "impact": f"{kind.title()} readiness is limited.",
        "scope": [kind.title()],
        "status": projection.get("state") or "unknown",
        "first_observed_at": None,
        "last_observed_at": projection.get("evidence_horizon"),
        "evidence_horizon": projection.get("evidence_horizon"),
        "owner": owner,
        "action": {"label": "Open owner", "permitted": True},
    }


def _capability_coverage(capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for capability in capabilities:
        active = capability.get("active") or {}
        if capability.get("availability") == "optional" and active.get("state") == "disabled":
            continue
        coverage = capability.get("coverage") or {}
        items.append(
            {
                "kind": "capability",
                "key": capability["key"],
                "state": active.get("state") or "unknown",
                "active": active,
                "pending": capability.get("pending"),
                "denominator": int(coverage.get("applicable") or 0),
                "complete": int(coverage.get("complete") or 0),
                "gaps": [
                    *(
                        item.get("message", "Capability exception")
                        for item in capability.get("exceptions") or []
                    ),
                    *(
                        item.get("message", "Capability blocker")
                        for item in capability.get("blockers") or []
                    ),
                ],
                "evidence_horizon": None,
                "owner": _global_owner_reference(
                    "project-settings",
                    "capabilities",
                    evidence_id=active.get("version_id"),
                ),
            }
        )
    return items


def _alert_signal_owner(item: dict[str, Any]) -> dict[str, Any] | None:
    """Where one governed alert firing opens, or `None` when nothing here holds it.

    WHY THIS EXISTS. `_signal_items` composed the alert item with no `owner` key at
    all, and `ProjectOverview.tsx` draws "Open evidence" only when one is present.
    The item therefore rendered, stated `_ALERT_SIGNAL_LIMITATION`, and went
    nowhere -- gap 5 of the audit of 2026-08-17, and exactly what `overview.md`
    forbids under `Incomplete if` ("an item cannot open its exact owning object").

    The destination is decided by WHAT THE FIRING IS ABOUT, which the row records
    in two columns this read model did not select until today:

    * `type = 'mediaplan_pace'` -- plan versus actual, whose console reading is the
      `pacing` lens of Analyze > Reports (story 67.26). The Datastream Workbench
      already composes exactly this reference
      (`datastream_workbench_placements.py`), so there is one destination for one
      reading and not a second one invented here.
    * `datastream_id` present -- every quality monitor stamps the Datastream its
      finding accuses (migration 229). The `runs` tab is the `datastream`
      contract's declared evidence tab (`ui/admin/src/shell/navigation/data.ts`),
      and it is where what arrived and when is recorded -- which is the fact a
      lateness, a volume drop or a zero-row window is about.
    * anything else -- `None`, and the caller says what is missing. Opening the
      Data Quality collection instead was the tempting move and it is refused:
      that lens lists `app.dq_monitors` rows, which are Datastream-scoped, so a
      Project-scoped `dq_geography` firing would land the reader on a list that
      does not contain their finding.

    MEASURED, not assumed. Under the owner DSN, every project-scoped firing of the
    last seven days is `dq_timeliness` and all of them carry a `datastream_id`
    (84 of 84 on 2026-08-21), so the second branch is the one production walks.
    """
    if str(item.get("alert_type") or "") == _MEDIAPLAN_PACE_ALERT_TYPE:
        # `lens` is set after construction, as the Workbench does: it is not one of
        # `_OWNER_FIELDS`, and the shell DROPS an unknown lens rather than refusing
        # the reference, so the Reports collection still opens if it is ever retired.
        owner = owner_reference("analyze", "reports")
        owner["lens"] = "pacing"
        return owner
    datastream_id = str(item.get("datastream_id") or "").strip()
    if datastream_id:
        return owner_reference(
            "data",
            "datastreams",
            object_type="datastream",
            object_id=datastream_id,
            tab="runs",
            evidence_id=str(item["id"]),
        )
    return None


def _signal_items(
    insights: dict[str, Any],
    renders: dict[str, Any],
    alerts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alerts = alerts or {"status": "empty", "items": []}
    items: list[dict[str, Any]] = []
    for item in insights.get("items") or []:
        # THE OWNER OF A DAILY INSIGHT IS ITS RENDER, NOT A RESULT.
        # This emitted `object_type="result", object_id=<daily_insight id>`. The
        # Result workbench loads `/analyze/results/{resultId}/...`
        # (`explorerClient.ts:377`); an id of `app.daily_insights` resolves to
        # nothing there, so "Open evidence" opened a Result that cannot exist --
        # a link false by construction, not by accident. The insight already
        # carries the addressable artifact it was rendered into.
        #
        # An insight with no render snapshot gets NO owner. The screen draws the
        # button only when `item.owner` is present (`ProjectOverview.tsx:222`),
        # so the item states its evidence and offers no dead click. Naming a
        # plausible-looking destination instead is the defect this repairs.
        snapshot_id = item.get("render_snapshot_id")
        owner = (
            owner_reference(
                "analyze",
                "renders",
                object_type="render",
                object_id=str(snapshot_id),
                evidence_id=str(snapshot_id),
            )
            if snapshot_id
            else None
        )
        signal = {
            **item,
            "kind": "validated_signal",
            "period": None,
            "freshness": item.get("created_at"),
            "limitations": (
                []
                if snapshot_id
                else ["This insight was not rendered, so it has no artifact to open."]
            ),
            "provenance": {"kind": "persisted_daily_insight", "id": item["id"]},
        }
        if owner:
            signal["owner"] = owner
        items.append(signal)
    for item in renders.get("items") or []:
        items.append(
            {
                **item,
                "kind": "render",
                "period": None,
                "freshness": item.get("freshness") or item.get("created_at"),
                "limitations": [],
                "provenance": {"kind": "persisted_render", "id": item["id"]},
                "owner": owner_reference(
                    "analyze",
                    "renders",
                    object_type="render",
                    object_id=item["id"],
                    evidence_id=item["id"],
                ),
            }
        )
    for item in alerts.get("items") or []:
        owner = _alert_signal_owner(item)
        signal = {
            **item,
            "kind": "governed_alert",
            "period": item.get("window_date"),
            "freshness": item.get("fired_at"),
            # The alert is one number against one threshold, never the whole
            # reading it came from -- true of every firing, whatever it opens.
            # A firing this console cannot open ADDS what is missing rather than
            # staying silent (`overview.md`, amendment of 2026-08-21).
            "limitations": (
                [_ALERT_SIGNAL_LIMITATION]
                if owner
                else [_ALERT_SIGNAL_LIMITATION, _ALERT_SIGNAL_NO_DESTINATION]
            ),
            "provenance": {
                "kind": f"alert_firing:{item.get('alert_type') or 'unknown'}",
                "id": item["id"],
            },
        }
        # Absent, never null: the screen draws the button on the KEY's presence
        # (`ProjectOverview.tsx`), so an owner of `None` would render a control
        # that opens nothing -- the defect this repairs, wearing a costume.
        if owner:
            signal["owner"] = owner
        items.append(signal)
    statuses = {insights.get("status"), renders.get("status"), alerts.get("status")}
    status = "ready" if items else "unavailable" if "unavailable" in statuses else "empty"
    outcomes = {
        "status": status,
        "items": items[:8],
        # THE WINDOW IS STATED, NOT IMPLIED. `_ALERT_SIGNAL_WINDOW_HOURS` said in a
        # comment that "the window is stated on the zone's evidence horizon", and no
        # field of this payload carried it: the zone's horizon is the max freshness
        # of the items, which is not a window. A reader could not tell an empty
        # alert list over seven days from one over seven minutes, and
        # `overview.md:123` requires that every count name its denominator AND its
        # window. It is now a field, and the screen prints it.
        "alert_window_hours": _ALERT_SIGNAL_WINDOW_HOURS,
    }
    # The disclosed silence of the daily-insight run travels with the outcomes, so the
    # posture below can say WHY a project has nothing to show instead of only THAT it
    # has nothing. It is carried, never counted: a blocked run adds no item and moves
    # no state -- `overview.md:189` forbids requiring insights for this surface, and
    # disclosing why one is silent is not requiring it.
    silence = insights.get("silence")
    if silence:
        outcomes["insight_silence"] = silence
    # A WITHDRAWN CLAIM TRAVELS TOO, and it is NOT an item. It left the signal list
    # because Overview volunteers what it lists; it arrives here so the screen can
    # show that something was said and unsaid, by whom and why. The two lists are
    # separate on purpose: merging them would put a retracted assertion back among
    # the assertions with a label, which is where a reader stops seeing labels.
    retractions = insights.get("retractions")
    if retractions:
        outcomes["insight_retractions"] = retractions
    return outcomes


def _business_silence_explanation(outcomes: dict[str, Any]) -> str:
    """The sentence a project with no business signal reads.

    "No persisted business signal is available." on its own is true and useless: it
    reads as a quiet project, and one of the three sources may in fact have been
    unable to look. Where the daily-insight run disclosed why it published nothing,
    that reason is APPENDED -- the absence is stated first, because it remains the
    fact, and then explained.
    """

    base = "No persisted business signal is available."
    silence = outcomes.get("insight_silence") or {}
    explanation = silence.get("explanation")
    return f"{base} {explanation}" if explanation else base


def compose_project_overview(
    project_id: str,
    conn,
    *,
    actor: str,
    can_edit: bool = False,
) -> dict[str, Any]:
    """Compose one authorized, read-only transverse Project posture envelope."""
    settings = _read_project_settings_projection(
        project_id,
        conn,
        can_edit=can_edit,
    )
    project = settings["project"]
    if str(project.get("id")) != project_id:
        raise ValueError("Project Settings scope mismatch")
    as_of = datetime.now(timezone.utc).isoformat()
    rows = _query_project_evidence(project_id, conn)
    summary, legacy_attention, active_work = derive_project_trust(rows)
    # THE SHARED PATH, NOT A SECOND ONE. This used to hand the composer
    # `rows[0]` -- the first Datastream of the fleet query above, ordered BY NAME
    # and including disabled ones -- so Overview and Getting Started could report
    # opposite readiness for the same Project (`overview.md:135` promises they do
    # not). `compose_project_readiness` owns the selection now, for both.
    readiness = compose_project_readiness(project_id, conn)
    governance = _query_governance_projection(project_id, conn)
    context = _query_context_projection(project_id, conn)
    test = _query_test_projection(project_id, conn)
    insights = _query_daily_insights(project_id, conn)
    renders = _query_recent_renders(project_id, conn)
    alert_signals = _query_recent_alert_firings(project_id, conn)
    outcomes = _signal_items(insights, renders, alert_signals)
    changes = _query_recent_changes(project_id, conn)

    operational_state = {
        "trusted": "ready",
        "attention": "degraded",
        "unknown": "unknown",
        "no_data": "unknown",
    }.get(summary["published_trust"], "unknown")
    operational = {
        "state": operational_state,
        "explanation": summary["evidence_message"],
        "evidence_horizon": summary.get("complete_through"),
        "owner": owner_reference("data", "data-overview"),
        "current_publication_available": summary["verified_datastreams"] > 0,
        "active_work": active_work,
    }
    readiness_states = [governance.get("state"), context.get("state"), test.get("state")]
    trust_state = (
        "degraded"
        if "degraded" in readiness_states
        else "unknown"
        if "unknown" in readiness_states
        else "ready"
    )
    trust = {
        "state": trust_state,
        "explanation": "Governance, Context and Test readiness remain separate evidence.",
        "evidence_horizon": max(
            [
                str(item.get("evidence_horizon"))
                for item in (governance, context, test)
                if item.get("evidence_horizon")
            ],
            default=None,
        ),
        "owner": governance["owner"],
    }
    business = {
        "state": "ready" if outcomes["items"] else "unknown",
        "explanation": (
            "Persisted business evidence is available."
            if outcomes["items"]
            else _business_silence_explanation(outcomes)
        ),
        "evidence_horizon": max(
            [str(item.get("freshness")) for item in outcomes["items"] if item.get("freshness")],
            default=None,
        ),
        "owner": owner_reference("analyze", "explore"),
    }
    limiting = next(
        (
            key
            for key, item in (
                ("operational_health", operational),
                ("trust_readiness", trust),
                ("business_signals", business),
            )
            if item["state"] != "ready"
        ),
        None,
    )

    data_coverage = {
        "kind": "data",
        "key": "publication",
        "state": operational_state,
        "active": {"state": summary["published_trust"], "version_id": None},
        "pending": None,
        "denominator": summary["active_datastreams"],
        "complete": summary["verified_datastreams"],
        "gaps": [item["detail"] for item in legacy_attention],
        "evidence_horizon": summary.get("complete_through"),
        "owner": operational["owner"],
    }
    projection_coverage = [
        {"kind": kind, "key": kind, **projection}
        for kind, projection in (
            ("governance", governance),
            ("context", context),
            ("test", test),
        )
    ]
    attention_candidates = _operational_attention(legacy_attention, as_of=as_of)
    for kind, projection in (
        ("governance", governance),
        ("context", context),
        ("test", test),
    ):
        item = _projection_attention(projection, kind)
        if item:
            attention_candidates.append(item)
    # Story 49.4 AC10: unresolved Control Cases, open DQ issues and in-force Rule
    # Set exceptions. Read-only, and keyed on the OWNER's root-cause fingerprint
    # so a case and the DQ issue that escalated into it collapse to one item
    # rather than being shown twice. Overview never copies or mutates the record.
    from core.controls_attention import control_attention_items  # noqa: PLC0415

    attention_candidates.extend(control_attention_items(conn, project_id=project["id"]))
    if not rows:
        attention_candidates.append(
            {
                "id": "setup:add-datastream",
                "root_cause_key": "setup:no-datastream",
                "priority_class": 5,
                "cause": "No Datastream is attached to this Project.",
                "impact": "Published data and first value are unavailable.",
                "scope": [project["name"]],
                "status": "open",
                "first_observed_at": None,
                "last_observed_at": as_of,
                "evidence_horizon": None,
                # The single Data-owned Wizard entry is an action on the
                # Datastreams collection: an empty Project has no object to name.
                "owner": owner_reference("data", "datastreams", action="create"),
                "action": {
                    "label": "Add Datastream",
                    "permitted": bool(project.get("can_edit")),
                },
            }
        )
    attention = rank_attention_items(attention_candidates)
    next_item = next(
        (item for item in attention if (item.get("action") or {}).get("permitted")),
        attention[0] if attention else None,
    )
    if not rows:
        next_item = next(
            item for item in attention if item["root_cause_key"] == "setup:no-datastream"
        )
    next_action = None
    if next_item:
        next_action = {
            "label": (next_item.get("action") or {}).get("label") or "Open owner",
            "permitted": bool((next_item.get("action") or {}).get("permitted")),
            "handoff": None
            if (next_item.get("action") or {}).get("permitted")
            else "Contact the responsible owner.",
            "owner": next_item["owner"],
            "cause": next_item["cause"],
        }

    return {
        "schema_version": PROJECT_OVERVIEW_SCHEMA_VERSION,
        "project": {
            "id": project["id"],
            "name": project["name"],
            "organization": project["organization"],
            "business_domains": project.get("business_domains") or [],
            "active_configuration_version_id": project.get("active_configuration_version_id"),
            "as_of": as_of,
        },
        "posture": {
            "operational_health": operational,
            "trust_readiness": trust,
            "business_signals": business,
            "limiting_dimension": limiting,
        },
        "next_action": next_action,
        "attention": {
            "items": attention[:8],
            "total": len(attention),
            "has_more": len(attention) > 8,
        },
        "coverage": [
            data_coverage,
            *_capability_coverage(settings.get("capabilities") or []),
            *projection_coverage,
        ],
        "outcomes": outcomes,
        "changes": changes,
        # RENDERED, NOT JUST COMPOSED. This field was in the envelope and in no
        # screen: `ProjectOverviewEnvelope` did not even declare it, so the setup
        # posture `overview.md:140` asks Overview to summarize was computed on every
        # request and thrown away. `components` names the order once, here, so the
        # screen iterates a list instead of hardcoding five keys that a sixth
        # component would silently miss.
        "readiness": {**readiness, "components": list(READINESS_COMPONENTS)},
    }


def _query_project_evidence(project_id: str, conn) -> list[dict]:
    """Load project-linked publication, verification and connection evidence."""
    health_stale_seconds = _health_stale_seconds()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ds.id,
                ds.name,
                ds.enabled,
                COALESCE(ds.source_kind, 'connector_pull') AS source_kind,
                ds.connection_ref_id,
                ch.status AS connection_status,
                ch.last_checked_at AS connection_last_checked_at,
                (
                    ch.last_checked_at >=
                    NOW() - (%s * INTERVAL '1 second')
                ) AS connection_health_current,
                cr.status AS connection_ref_status,
                cr.enabled AS connection_enabled,
                scope.state AS account_state,
                accounts.available AS account_available,
                (
                    cr.owner_org_id = pf.org_id
                    OR EXISTS (
                        SELECT 1
                        FROM app.credential_account_grants grants
                        WHERE grants.credential_id = cr.id
                          AND grants.external_account_id = scope.account_id
                          AND grants.grantee_org_id = pf.org_id
                          AND grants.status = 'active'
                    )
                ) AS account_exposed,
                ds.current_published_execution_id,
                published.state AS publication_state,
                published.state_changed_at AS published_at,
                published.row_count AS published_row_count,
                latest.state AS latest_pull_state,
                latest.date_to AS latest_pull_date,
                latest.verdict AS latest_verdict,
                latest.completed_at AS latest_pull_completed_at,
                recent.state AS coverage_state,
                recent.interval_start AS coverage_interval_start,
                recent.interval_end_exclusive AS coverage_end_exclusive,
                recent.execution_id AS coverage_execution_id,
                coverage_verdict.verdict AS coverage_verdict,
                recent.covered_at AS verified_at,
                active.state AS active_state
            FROM app.project_flux pf
            JOIN app.datastreams ds
              ON ds.id = pf.flux_id
             AND ds.org_id = pf.org_id
            LEFT JOIN app.connection_ref cr
              ON cr.id = ds.connection_ref_id
            LEFT JOIN app.connection_health ch
              ON ch.connection_ref_id = ds.connection_ref_id
            -- THE ACCOUNT THIS DATASTREAM READS. Both joins used to hang off
            -- the credential alone, so every Datastream of one authorization
            -- reported the state of whichever single account that authorization
            -- had selected -- and since migration 211 allows several, the same
            -- join would have printed one row per account per Datastream.
            -- `source_account_id` is the Datastream's own answer; a row that
            -- predates 211 and names none reports no account state, which is
            -- true rather than borrowed.
            LEFT JOIN app.credential_accounts accounts
              ON accounts.credential_id = ds.connection_ref_id
             AND accounts.source_account_id = ds.source_account_id
            LEFT JOIN app.connection_account_scope scope
              ON scope.connection_ref_id = ds.connection_ref_id
             AND scope.account_id = accounts.external_account_id
            LEFT JOIN app.datastream_executions published
              ON published.id = ds.current_published_execution_id
             AND published.datastream_id = ds.id
             AND published.project_id = ds.project_id
            LEFT JOIN LATERAL (
                SELECT pj.state, pj.date_to, pj.completed_at, pv.verdict
                FROM app.pull_jobs pj
                LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                WHERE pj.datastream_id = ds.id
                ORDER BY pj.enqueued_at DESC
                LIMIT 1
            ) latest ON true
            LEFT JOIN app.datastream_coverage recent
              ON recent.datastream_id = ds.id
             AND recent.project_id = ds.project_id
             AND recent.coverage_kind = 'recent'
            LEFT JOIN app.pull_verifications coverage_verdict
              ON coverage_verdict.pull_id = recent.pull_id
            LEFT JOIN LATERAL (
                SELECT state
                FROM app.datastream_executions
                WHERE datastream_id = ds.id
                  AND project_id = ds.project_id
                  AND state IN ('created', 'loading', 'validating', 'ready', 'publishing')
                ORDER BY state_changed_at DESC
                LIMIT 1
            ) active ON true
            WHERE pf.project_id = %s
              AND ds.archived_at IS NULL
            ORDER BY ds.name ASC
            """,
            (health_stale_seconds, project_id),
        )
        cols = [str(item[0]) for item in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def derive_project_trust(rows: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Derive trust; unknown or adverse evidence always wins over healthy."""
    active_rows = [row for row in rows if bool(row.get("enabled"))]
    if not active_rows:
        empty_project = not rows
        return (
            {
                "published_trust": "no_data",
                "complete_through": None,
                "active_datastreams": 0,
                "no_data_reason": ("empty_project" if empty_project else "no_active_datastreams"),
                "verified_datastreams": 0,
                "attention_count": 0,
                "active_work_count": 0,
                "evidence_message": (
                    "Add a Datastream to establish published-data trust."
                    if empty_project
                    else "No Datastream is active, so published-data trust is unavailable."
                ),
            },
            [],
            [],
        )

    attention: list[dict] = []
    active_work: list[dict] = []
    verified_dates: list[str] = []
    unknown_ids: set[str] = set()
    adverse_ids: set[str] = set()

    for row in active_rows:
        datastream_id = str(row["id"])
        name = str(row["name"])
        missing: list[str] = []
        source_kind = str(row.get("source_kind") or "connector_pull")
        connection_status = row.get("connection_status")
        connection_evidence_missing = False

        current_execution_id = row.get("current_published_execution_id")
        has_current_publication = bool(
            current_execution_id and row.get("publication_state") == "published"
        )
        verified_through = _inclusive_coverage_date(row.get("coverage_end_exclusive"))
        coverage_current = (
            has_current_publication
            and row.get("coverage_state") == "covered"
            and row.get("coverage_interval_start") is not None
            and verified_through is not None
            and row.get("verified_at") is not None
            and row.get("coverage_execution_id") == current_execution_id
        )
        if not has_current_publication:
            missing.append("a current published execution")
        if not coverage_current:
            missing.append("verified recent coverage for the current publication")
        if source_kind == "connector_pull":
            if coverage_current and row.get("coverage_verdict") != "ok":
                missing.append("a successful completeness verification")
            connection_issues: list[str] = []
            if not row.get("connection_ref_id"):
                connection_evidence_missing = True
                missing.append("a source connection")
            else:
                ref_status = row.get("connection_ref_status")
                if ref_status is None:
                    connection_evidence_missing = True
                    missing.append("connection status evidence")
                elif ref_status != "active":
                    connection_issues.append("revoked")
                if row.get("connection_enabled") is None:
                    connection_evidence_missing = True
                    missing.append("connection enablement evidence")
                elif row.get("connection_enabled") is not True:
                    connection_issues.append("disabled")
                if connection_status is None:
                    connection_evidence_missing = True
                    missing.append("connection-health evidence")
                elif connection_status != "ok":
                    connection_issues.append(f"health {connection_status}")
                elif row.get("connection_health_current") is not True:
                    connection_evidence_missing = True
                    missing.append("recent connection-health evidence")
                if row.get("account_state") is None:
                    connection_evidence_missing = True
                    missing.append("selected-account evidence")
                elif row.get("account_state") != "ready":
                    connection_issues.append("account not ready")
                if row.get("account_available") is None:
                    connection_evidence_missing = True
                    missing.append("account availability evidence")
                elif row.get("account_available") is not True:
                    connection_issues.append("account unavailable")
                if row.get("account_exposed") is None:
                    connection_evidence_missing = True
                    missing.append("account exposure evidence")
                elif row.get("account_exposed") is not True:
                    connection_issues.append("account exposure revoked")
            if connection_issues:
                adverse_ids.add(datastream_id)
                attention.append(
                    _attention(
                        row,
                        "connection_unusable",
                        f"Connection is {', '.join(connection_issues)}.",
                        "overview",
                    )
                )

        latest_state = row.get("latest_pull_state")
        latest_verdict = row.get("latest_verdict")
        coverage_state = row.get("coverage_state")
        if coverage_state in {"degraded", "failed"}:
            adverse_ids.add(datastream_id)
            attention.append(
                _attention(
                    row,
                    "coverage_incomplete",
                    f"Recent coverage is {coverage_state}.",
                    "runs",
                )
            )

        latest_is_newer = _is_newer_evidence(
            row.get("latest_pull_completed_at"), row.get("published_at")
        )
        if latest_is_newer and latest_state in {"failed", "dead_letter"}:
            adverse_ids.add(datastream_id)
            if coverage_current:
                detail = (
                    "The latest run failed; the verified current publication remains available."
                )
            elif has_current_publication:
                detail = (
                    "The latest run failed; the current publication remains available, "
                    "but its coverage horizon cannot be verified."
                )
            else:
                detail = "The latest run failed; no current publication is available."
            attention.append(
                _attention(
                    row,
                    "latest_run_failed",
                    detail,
                    "runs",
                )
            )
        elif latest_is_newer and latest_verdict in {"partial", "empty"}:
            adverse_ids.add(datastream_id)
            attention.append(
                _attention(
                    row,
                    "verification_incomplete",
                    f"The latest verification is {latest_verdict}.",
                    "runs",
                )
            )

        if missing:
            unknown_ids.add(datastream_id)
            attention.append(
                _attention(
                    row,
                    "evidence_missing",
                    f"Needs {', '.join(missing)}.",
                    (
                        "first-publication"
                        if not has_current_publication
                        else "overview"
                        if connection_evidence_missing
                        else "runs"
                    ),
                )
            )
        else:
            verified_dates.append(verified_through)

        if row.get("active_state"):
            active_work.append(
                {
                    "datastream_id": datastream_id,
                    "name": name,
                    "state": str(row["active_state"]),
                    "target": "runs",
                }
            )

    verified_count = len(active_rows) - len(unknown_ids)
    complete_through = min(verified_dates) if verified_count == len(active_rows) else None
    if adverse_ids:
        trust = "attention"
        if unknown_ids:
            evidence_message = (
                "Required evidence is missing and newer evidence also requires review."
            )
        else:
            evidence_message = (
                "Verified current publications remain available, "
                "with newer evidence requiring review."
            )
    elif unknown_ids:
        trust = "unknown"
        evidence_message = "Required publication, verification or connection evidence is missing."
    else:
        trust = "trusted"
        evidence_message = "Every active Datastream has current publication and verified coverage."

    return (
        {
            "published_trust": trust,
            "complete_through": complete_through,
            "active_datastreams": len(active_rows),
            "no_data_reason": None,
            "verified_datastreams": verified_count,
            "attention_count": len(adverse_ids | unknown_ids),
            "active_work_count": len(active_work),
            "evidence_message": evidence_message,
        },
        attention,
        active_work,
    )


def _attention(row: dict, reason: str, detail: str, target: str) -> dict:
    return {
        "datastream_id": str(row["id"]),
        "name": str(row["name"]),
        "reason": reason,
        "detail": detail,
        "target": target,
    }


def _query_latest_test(project_id: str, conn) -> dict:
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT score_passed, score_total, regressions, status, run_at
                    FROM app.eval_runs
                    WHERE project_id = %s
                    ORDER BY run_at DESC
                    LIMIT 1
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.warning("overview: latest_test_unavailable project=%s: %s", project_id, exc)
        return {"status": "unavailable", "value": None}
    if row is None:
        return {"status": "empty", "value": None}
    return {
        "status": "ready",
        "value": {
            "passed": int(row[0]),
            "total": int(row[1]),
            "regressions": int(row[2]),
            "result": str(row[3]),
            "run_at": _iso_value(row[4]),
        },
    }


#: Phrases servies quand le dernier run d'insight n'a rien publie. Le SILENCE EST
#: UN ETAT ET IL SE DIT (`proactive-assertions.md`, << Silence is a state, and it
#: is disclosed >>) : la surface qui ne peut pas parler dit pourquoi, au lieu de
#: se lire comme une surface qui n'avait rien a dire.
_INSIGHT_SILENCE = {
    "blocked": "The daily insight run was blocked: the day's data was not ready.",
    "no_insight": "The daily insight run found nothing worth reporting that day.",
    "failed": "The daily insight run failed before it could report.",
    # THE FIFTH STATE (`execution-substrate.md`, host-scheduled work). The four
    # above are things a run RECORDED. An absent row is not one of them: it says
    # the task never ran, and toorow owns neither its clock nor its host. This
    # answered `empty` -- the same word as `no_insight` -- so a Project whose
    # scheduled task was never installed read exactly like a Project whose task
    # ran and found the day quiet. That is `Incomplete if` 11 of that page in its
    # second form: the silence of a unit scheduled in someone else's host read as
    # a run that happened.
    "never_ran": (
        "No daily insight run has ever been recorded for this project, so the "
        "scheduled task has not reported once."
    ),
}

#: The gesture that repairs the fifth state. It is the only silence here a person
#: can act on, and the sentence names the act rather than the absence -- and never
#: an environment, a queue or a deployment: this task runs in the operator's own
#: LLM host, which toorow does not deploy to.
_NEVER_RAN_GESTURE = (
    "Copy the daily insight task recipe from Project settings, General, into your "
    "LLM host's scheduled task -- or run it there once to see the first day appear."
)


def _insight_silence(status: str, coverage: object, insight_date: object) -> dict | None:
    """Name why the last daily-insight run published nothing -- or return None.

    The publication path RECORDS this: a `no_insight` declared on a day readiness
    called blocked is written as `blocked`, with the server-measured reasons in the
    run's coverage manifest. Reading `status` and dropping `coverage` collapsed all
    of it back into one word one layer up, and the Overview then said "No persisted
    business signal is available" -- which is the sentence that rule exists to
    forbid. Nothing happened, and nobody could look, are not the same day.
    """

    sentence = _INSIGHT_SILENCE.get(status)
    if sentence is None:
        return None
    if status == "never_ran":
        # No row means no coverage manifest and no date -- there is nothing to
        # read, and inventing either would be the back-filled "presumed ran" the
        # substrate page forbids by name. What IS added is the repair.
        return {
            "state": status,
            "insight_date": None,
            "explanation": f"{sentence} {_NEVER_RAN_GESTURE}",
            "reason": None,
            "gesture": _NEVER_RAN_GESTURE,
        }
    reason = None
    if isinstance(coverage, str):
        try:
            coverage = json.loads(coverage)
        except json.JSONDecodeError:
            coverage = None
    if isinstance(coverage, dict):
        raw = coverage.get("reason")
        reason = str(raw) if raw else None
    return {
        "state": status,
        "insight_date": str(insight_date) if insight_date else None,
        "explanation": f"{sentence} {reason}" if reason else sentence,
        "reason": reason,
    }


def _insight_retraction_silence(retractions: list[dict[str, Any]]) -> dict:
    """Why the latest run shows nothing when every one of its claims was withdrawn.

    The reason is the AUTHOR'S, quoted rather than summarised: it is the whole
    content of a retraction, and the surface that hides it turns an audited
    withdrawal back into a disappearance.
    """

    first = retractions[0]
    who = first.get("retracted_by") or "someone in this project"
    reason = first.get("reason")
    sentence = (
        f"The {len(retractions)} insight(s) published for "
        f"{first.get('insight_date') or 'that day'} were retracted by {who}."
        if len(retractions) > 1
        else (
            f"The insight published for {first.get('insight_date') or 'that day'} "
            f"was retracted by {who}."
        )
    )
    return {
        "state": "retracted",
        "insight_date": first.get("insight_date"),
        "explanation": f"{sentence} {reason}" if reason else sentence,
        "reason": reason,
    }


def _query_daily_insights(project_id: str, conn) -> dict:
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_run AS (
                        SELECT id, status, coverage, insight_date
                        FROM app.daily_insight_runs
                        WHERE project_id = %s
                        ORDER BY insight_date DESC
                        LIMIT 1
                    )
                    SELECT r.status, i.id, i.payload, i.created_at,
                           i.render_snapshot_id, r.coverage, r.insight_date,
                           i.retracted_at, i.retracted_by, i.retracted_reason
                    FROM latest_run r
                    LEFT JOIN app.daily_insights i ON i.run_id = r.id
                    ORDER BY i.slot ASC
                    LIMIT 3
                    """,
                    (project_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("overview: daily_insights_unavailable project=%s: %s", project_id, exc)
        return {"status": "unavailable", "items": []}

    if not rows:
        # THE FIFTH STATE, and it is not `empty`. `empty` is the word this function
        # also returned for `no_insight`, so an absent run -- the task never ran in
        # the operator's host -- was indistinguishable from a run that looked and
        # found nothing. `execution-substrate.md` states the four recorded states
        # plus the absent row and forbids collapsing any pair of them.
        return {
            "status": "never_ran",
            "items": [],
            "silence": _insight_silence("never_ran", None, None),
        }
    latest_status = str(rows[0][0])
    silence = _insight_silence(latest_status, rows[0][5], rows[0][6])
    if latest_status in {"blocked", "failed"}:
        return {"status": "unavailable", "items": [], "silence": silence}
    if latest_status == "no_insight":
        return {"status": "empty", "items": [], "silence": silence}
    if latest_status != "published":
        return {"status": "unavailable", "items": []}

    # A CONFIDENCE WORD NEVER TRAVELS ALONE OUT OF THIS FUNCTION.
    # `insight.confidence` is DECLARED by the model; its only check is enum
    # membership (`daily_insights_schema:147-151`). Story 53.4 removed that
    # ambiguity by making every published payload carry an `authorship` block that
    # says so -- and this projection dropped it, so `outcomes.items[].confidence`
    # reached every consumer of `GET /api/projects/{id}/overview` as a bare word
    # with nobody named as its author. `overview.md:277` refuses exactly that: a
    # surfaced signal must not state a confidence level no server evidence backs.
    # The block is DERIVED by the same function the Daily Insights routes call
    # (`authorship_of`), never re-implemented here.
    from core.daily_insights_schema import authorship_of  # noqa: PLC0415

    items = []
    retractions: list[dict[str, Any]] = []
    for row in rows:
        (
            _status,
            insight_id,
            raw_payload,
            created_at,
            snapshot_id,
            _cov,
            _date,
            retracted_at,
            retracted_by,
            retracted_reason,
        ) = row
        if insight_id is None:
            continue
        payload = raw_payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        editorial = payload.get("insight") if isinstance(payload, dict) else None
        if not isinstance(editorial, dict) or not editorial.get("title"):
            continue
        if retracted_at is not None:
            # A RETRACTED CLAIM IS NOT VOLUNTEERED AGAIN, AND IT DOES NOT VANISH
            # (migration 321). Overview is the surface that ASSERTS: carrying the
            # item here would keep saying what its author withdrew. Dropping it in
            # silence would be the delete `proactive-assertions.md` decision 4
            # refuses, one layer up from the database -- the reader could not tell
            # "never said" from "said, then withdrawn". So it leaves the signals
            # and arrives as a withdrawal, with who, when and why.
            retractions.append(
                {
                    "id": str(insight_id),
                    "title": str(editorial["title"]),
                    "insight_date": str(_date) if _date else None,
                    "retracted_at": _iso_value(retracted_at),
                    "retracted_by": str(retracted_by) if retracted_by else None,
                    "reason": str(retracted_reason) if retracted_reason else None,
                }
            )
            continue
        authorship = authorship_of(payload)
        derived_reading = (authorship.get("derivedConfidence") or {}).get("reading")
        items.append(
            {
                "id": str(insight_id),
                "title": str(editorial["title"]),
                "summary": str(editorial.get("summary") or ""),
                # THE READING, never the model's bare word (overview.md: a signal
                # must not state a confidence no server evidence backs). The
                # declared word still travels, under its own name, inside
                # `authorship.declaredConfidence`; a consumer that reads only
                # `confidence` gets the server's measurement or `unmeasurable`.
                "confidence": str(derived_reading or "unmeasurable"),
                "authorship": authorship,
                "created_at": _iso_value(created_at),
                "render_snapshot_id": str(snapshot_id) if snapshot_id else None,
            }
        )
    if items:
        return {"status": "ready", "items": items, "retractions": retractions}
    if retractions:
        # Every claim of the latest run was withdrawn. That is neither "nothing was
        # published" nor "nobody could look": it is a day that spoke and unsaid it,
        # and the posture says which.
        return {
            "status": "empty",
            "items": [],
            "retractions": retractions,
            "silence": _insight_retraction_silence(retractions),
        }
    return {"status": "unavailable", "items": []}


def _query_recent_renders(project_id: str, conn) -> dict:
    try:
        from core.snapshots import list_render_snapshots

        with conn.transaction():
            rows = list_render_snapshots(project_id, conn, limit=3)
    except Exception as exc:
        logger.warning("overview: renders_unavailable project=%s: %s", project_id, exc)
        return {"status": "unavailable", "items": []}
    items = [
        {
            "id": str(row["id"]),
            "kind": str(row["tool_name"]),
            "title": str(row.get("question") or row.get("summary_snippet") or "Saved render"),
            "created_at": row.get("created_at"),
            "freshness": (
                (row.get("meta") or {}).get("freshness")
                if isinstance(row.get("meta"), dict)
                else None
            ),
        }
        for row in rows
    ]
    return {"status": "ready" if items else "empty", "items": items}


def _query_recent_alert_firings(project_id: str, conn) -> dict:
    """Read the governed alert firings of one Project, every family at once.

    WHY THIS EXISTS. `overview.md:57` lists a **governed alert** among the
    evidence a business signal is made of, and the zone read none: `_signal_items`
    joined published daily insights and saved renders only. Three writers fill
    `app.alert_firings` -- `business_alerts` (threshold), `anomaly_alerts` and
    `mediaplan_alerts` (plan pace) -- and every one of them reached the MCP daily
    report (`server/core/reporting_mcp.py#get_daily_report`) and the briefing,
    never the console. Reading the table
    by `project_id` rather than by family repairs the three together; a fourth
    family surfaces the day it writes a row, with no edit here.

    Media-plan pacing is the reason it was measured: its console screen was
    removed with `ui/admin/src/mediaplans/`, so the firing is the only thing a
    person can still see of a plan's pace outside the MCP App
    (`analyze-and-test.md`, Plan-versus-actual). That is exactly why the caller
    stamps `_ALERT_SIGNAL_LIMITATION` on the item: a threshold breach is not the
    reading it came from, and an unqualified alert would read as the whole answer.

    Degrades like its neighbours: an unreachable table is `unavailable`, never an
    empty list a reader would take for "nothing fired".
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, type, metric, severity, message,
                           observed_value, threshold, window_date, fired_at,
                           datastream_id
                    FROM app.alert_firings
                    WHERE project_id = %s
                      AND fired_at >= NOW() - make_interval(hours => %s)
                    ORDER BY fired_at DESC
                    LIMIT %s
                    """,
                    (project_id, _ALERT_SIGNAL_WINDOW_HOURS, _ALERT_SIGNAL_LIMIT),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("overview: alert_firings_unavailable project=%s: %s", project_id, exc)
        return {"status": "unavailable", "items": []}
    items = []
    for row in rows:
        (
            firing_id,
            alert_type,
            metric,
            severity,
            message,
            observed_value,
            threshold,
            window_date,
            fired_at,
            datastream_id,
        ) = row
        # The writers append a metadata JSON blob after " | "; only the readable
        # half is a title (`mediaplan_alerts.fetch_recent_mediaplan_firings`).
        readable = str(message or "").split(" | ")[0].strip()
        items.append(
            {
                "id": str(firing_id),
                "alert_type": str(alert_type or "unknown"),
                "title": readable or str(metric or "") or "Governed alert",
                "metric": str(metric or ""),
                "severity": str(severity or "warning"),
                "observed_value": float(observed_value) if observed_value is not None else None,
                "threshold": float(threshold) if threshold is not None else None,
                "window_date": _iso_value(window_date),
                "fired_at": _iso_value(fired_at),
                # WHAT THE FIRING IS ABOUT, and therefore what the item opens.
                # Migration 229 added this column and this read model never
                # selected it, so `_signal_items` had nothing to build a
                # destination from and the item opened nothing (audit of
                # 2026-08-17, gap 5). NULL on the kinds that name no Datastream
                # and on every row written before 229 -- carried as `None`, never
                # as an empty string a resolver would take for an id.
                "datastream_id": str(datastream_id) if datastream_id else None,
            }
        )
    return {"status": "ready" if items else "empty", "items": items}


def _iso_value(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _inclusive_coverage_date(value) -> str | None:
    """Convert a persisted UTC exclusive bound to its last complete day."""
    if value is None:
        return None
    if isinstance(value, datetime):
        point = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        end_date = point.astimezone(timezone.utc).date()
    elif isinstance(value, date):
        end_date = value
    else:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if point.tzinfo is None:
            point = point.replace(tzinfo=timezone.utc)
        end_date = point.astimezone(timezone.utc).date()
    return (end_date - timedelta(days=1)).isoformat()


def _health_stale_seconds() -> int:
    """Reuse the canonical health-poller staleness threshold, safely bounded."""
    try:
        value = int(os.environ.get("ALERT_HEALTH_POLLER_STALE_SECONDS", "7200"))
    except ValueError:
        value = 7200
    return min(max(value, 60), 604800)


def _as_utc_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, date):
        point = datetime.combine(value, datetime.min.time())
    else:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _is_newer_evidence(candidate, reference) -> bool:
    """Treat unknown timing conservatively; ignore only demonstrably older evidence."""
    candidate_at = _as_utc_datetime(candidate)
    reference_at = _as_utc_datetime(reference)
    if candidate_at is None or reference_at is None:
        return True
    return candidate_at > reference_at
