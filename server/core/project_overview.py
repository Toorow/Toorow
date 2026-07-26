"""Authoritative Project Overview read model (Epic 43, Story 43.15).

This module composes only project-scoped governance evidence. It does not query
analytical facts or infer business metrics in the browser.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def authorize_overview_project(identity: str, project_id: str, conn) -> bool:
    """Fail-closed project access check shared by both overview routes."""
    from core.project_access import (
        epic36_production_access_enabled,
        identity_has_project_access,
        resolve_strict_resource_access,
    )

    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    strict = auth_mode != "disabled"
    production_rls = epic36_production_access_enabled(auth_mode=auth_mode)
    if strict:
        from core.db import set_local_access_context

        set_local_access_context(conn, identity, enforce_epic36=production_rls)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.projects WHERE id = %s AND status = 'active'",
            (project_id,),
        )
        if cur.fetchone() is None:
            return False

    if strict:
        return resolve_strict_resource_access(
            identity,
            conn,
            project_id=project_id,
            minimum_capability="view",
            auth_mode=auth_mode,
        ).allowed
    return identity_has_project_access(
        project_id,
        identity or "anonymous",
        conn,
        fail_closed=True,
    )


def build_project_overview(project_id: str, conn, operational: dict) -> dict:
    """Build the Project Overview without converting missing evidence to health."""
    rows = _query_project_evidence(project_id, conn)
    summary, attention, active_work = derive_project_trust(rows)
    return {
        **operational,
        "summary": {
            **summary,
            "latest_test": _query_latest_test(project_id, conn),
        },
        "attention": attention[:8],
        "attention_total": len(attention),
        "active_work": active_work[:8],
        "daily_insights": _query_daily_insights(project_id, conn),
        "recent_renders": _query_recent_renders(project_id, conn),
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
            LEFT JOIN app.connection_account_scope scope
              ON scope.connection_ref_id = ds.connection_ref_id
            LEFT JOIN app.credential_accounts accounts
              ON accounts.credential_id = ds.connection_ref_id
             AND accounts.external_account_id = scope.account_id
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
                "no_data_reason": (
                    "empty_project" if empty_project else "no_active_datastreams"
                ),
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


def _query_daily_insights(project_id: str, conn) -> dict:
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest_run AS (
                        SELECT id, status
                        FROM app.daily_insight_runs
                        WHERE project_id = %s
                        ORDER BY insight_date DESC
                        LIMIT 1
                    )
                    SELECT r.status, i.id, i.payload, i.created_at,
                           i.render_snapshot_id
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
        return {"status": "empty", "items": []}
    latest_status = str(rows[0][0])
    if latest_status in {"blocked", "failed"}:
        return {"status": "unavailable", "items": []}
    if latest_status == "no_insight":
        return {"status": "empty", "items": []}
    if latest_status != "published":
        return {"status": "unavailable", "items": []}

    items = []
    for _status, insight_id, raw_payload, created_at, snapshot_id in rows:
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
        items.append(
            {
                "id": str(insight_id),
                "title": str(editorial["title"]),
                "summary": str(editorial.get("summary") or ""),
                "confidence": str(editorial.get("confidence") or "unknown"),
                "created_at": _iso_value(created_at),
                "render_snapshot_id": str(snapshot_id) if snapshot_id else None,
            }
        )
    return {"status": "ready", "items": items} if items else {
        "status": "unavailable",
        "items": [],
    }

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


def _iso_value(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _inclusive_coverage_date(value) -> str | None:
    """Convert a persisted UTC exclusive bound to its last complete day."""
    if value is None:
        return None
    if isinstance(value, datetime):
        point = (
            value
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
        end_date = point.astimezone(timezone.utc).date()
    elif isinstance(value, date):
        end_date = value
    else:
        try:
            point = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
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
