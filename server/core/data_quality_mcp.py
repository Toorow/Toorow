"""Story 13.4 -- `get_data_quality_report`, the core-owned data-quality read.

AD-1 dual channel: a <=30-line text summary on the LLM channel plus the
structured envelope (monitors[], issues[], freshness, provenance) on
structuredContent. AD-5: project scope is resolved before any DB read. AD-9:
every issue carries its firing_id and pull_ids. Degraded is honest -- zero
monitors evaluated or a database that does not answer produce a summary that
says so, never an exception.

The `from core.main import ...` line inside the body is the seam every extracted
surface uses: `core.main` imports this module, so a module-level import back
would be a cycle -- and `_resolve_project` and `get_access_token` are patched by
`test_dq_mcp_tool` at the `core.main` address.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

logger = logging.getLogger(__name__)




def get_data_quality_report(
    project_id: str = "default",
    connector: str | None = None,
) -> ToolResult:
    """Data quality health report for a project -- lean summary + full detail (AD-1).

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)
    # AD-5: project scoping enforced -- caller sees only its own project issues.
    # AD-9: every issue carries firing_id (primary provenance) and pull_ids
    #        (best-effort; typically empty for aggregated DQ monitors because
    #        infra_alerts.write_infra_firing hardcodes pull_ids='{}').

    Returns a <=30-line plain-text summary on the LLM channel (AD-1) with:
      - intro: project + evaluation window
      - one line per DQ monitor (label, % healthy, N unresolved issues)
      - freshness: date of last successful extraction
      - total open issues + top-3 most-recent

    The full canonical AD-1 envelope rides structuredContent:
      data.monitors[]:  [{type, label, healthy_pct, unresolved_count}]
      data.issues[]:    [{firing_id, type, datastream_id, datastream_name,
                          module_name, message, fired_at, severity, pull_ids}]
        Note: firing_id is the primary provenance key (app.alert_firings PK).
              pull_ids reflects the raw TEXT[] from Postgres -- empty [] for
              most DQ firings (aggregated monitors don't track individual pulls).
      meta.freshness:   last_pull_at | "no evaluation"
      meta.provenance:  {source_system, source_field, pull_id}
      meta.alerts:      []  (DQ issues live in data.issues, not meta.alerts)

    Degraded path: if no DQ data exists (project new, DB hiccup) the tool
    returns an honest summary ("aucune evaluation disponible") with zero issues
    and never raises an exception to the agent.  If the firing-count query fails
    but raw issues could be read, the summary explicitly flags monitors as
    unavailable and derives total_unresolved from the issue list (H1 coherence).

    Parameters:
        project_id: Project identifier (default: 'default').
        connector:  Optional Connector name to filter (e.g. "google-ads").
                    When None, every Connector in the project is included.
                    AD-2: opaque -- no Connector names are hardcoded here.
    """
    from core import db as _core_db  # noqa: PLC0415
    from core.dq_api import _MONITOR_LABELS, fetch_dq_report_data  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _resolve_project,
        get_access_token,
    )
    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_can_read_project,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id, identity)

    def _denied_response(reason: str = "denied") -> ToolResult:
        """Return a non-disclosing, non-error response for access denied / unavailable."""
        _summary = f"Project {project_id!r} not found or access denied."
        _env = _envelope(
            {
                "project_id": project_id,
                "connector": connector,
                "monitors": [],
                "issues": [],
                "total_unresolved": 0,
                "evaluated_days_30d": 0,
            },
            provenance={
                "source_system": "connector-core",
                "source_field": "get_data_quality_report",
                "pull_id": None,
            },
            freshness=reason,
        )
        return ToolResult(
            content=[TextContent(type="text", text=_summary)],
            structured_content=_env,
        )

    # AD-5: enforce project scope before any DB read.
    # fail_closed=True: if the scope DB is unavailable we cannot confirm access
    # for a cross-project governed resource -- return the same non-disclosing
    # response as an explicit denial (M1 fix).
    # Exception: "anonymous" identity short-circuits inside identity_can_read_project
    # (single-tenant dev path) and never touches the DB, so fail_closed is safe.
    try:
        with _core_db.request_connection(identity) as _conn_scope:
            if not identity_can_read_project(
                project_id, identity, _conn_scope, fail_closed=True
            ):
                return _denied_response("denied")
    except ProjectAccessUnavailable as _pau:
        logger.debug(
            "get_data_quality_report: scope_check_unavailable project=%s: %s",
            project_id,
            _pau,
        )
        return _denied_response("scope_unavailable")
    except Exception as _scope_exc:  # noqa: BLE001 -- e.g. get_connection itself fails
        logger.debug(
            "get_data_quality_report: scope_connection_failed project=%s: %s",
            project_id,
            _scope_exc,
        )
        return _denied_response("scope_unavailable")

    # Fetch DQ data (resilient: returns partial/empty on any query failure).
    dq_data: dict = {
        "monitors": [],
        "issues": [],
        "freshness_last_pull_at": None,
        "evaluated_days_30d": 0,
        "total_unresolved": 0,
    }
    try:
        with _core_db.request_connection(identity) as _conn_dq:
            dq_data = fetch_dq_report_data(project_id, _conn_dq, module=connector)
    except Exception as _fetch_exc:  # noqa: BLE001 -- degraded path
        logger.debug("get_data_quality_report: fetch_skipped: %s", _fetch_exc)

    monitors = dq_data.get("monitors") or []
    issues = dq_data.get("issues") or []
    freshness_ts = dq_data.get("freshness_last_pull_at")
    evaluated_days = dq_data.get("evaluated_days_30d", 0)
    total_unresolved = dq_data.get("total_unresolved", 0)
    monitors_unavailable = dq_data.get("monitors_unavailable", False)

    # ------------------------------------------------------------------
    # Build the <=30-line LLM-channel summary (AD-1).
    # Line budget: 1 header + 1 window + 5 monitors + 1 freshness + 1 blank
    #              + 1 total + up to 3 top issues + 1 footer = <=14 lines max.
    # ------------------------------------------------------------------
    lines: list[str] = []

    # STORY 59.5 -- THIS BLOCK IS ENGLISH. It renders `_MONITOR_LABELS`, whose six
    # entries were French until the monitor registry took them over; a translated
    # label wrapped in a French sentence would have moved the defect rather than
    # repaired it. The labels now come from `core.dq_monitor_registry` and the
    # sentences around them are the repository's language.
    connector_suffix = f" (connector: {connector})" if connector else ""
    lines.append(f"Data quality report -- project {project_id!r}{connector_suffix}")
    lines.append(f"Window: last 30 days (days evaluated: {evaluated_days})")

    if not monitors:
        # Degraded: no monitor data at all.
        if monitors_unavailable and issues:
            # H1 coherence: monitors query failed but raw issues were read.
            # Explicitly signal the partial state rather than claiming "0 issues".
            lines.append(
                f"Monitors unavailable (partial database error) -- "
                f"{len(issues)} raw issue(s) read."
            )
        else:
            lines.append("No monitor evaluation available.")
    else:
        for m in monitors:
            label = m.get("label", m.get("type", "?"))
            # `healthy_pct` is PRESENT and None when no day was evaluated, so the
            # `.get` default never fires. Zero evaluated days is "not evaluated",
            # never "100% healthy" -- and it must not crash the summary either.
            healthy_pct = m.get("healthy_pct")
            unresolved = m.get("unresolved_count", 0)
            issue_word = "issue" if unresolved <= 1 else "issues"
            health = "not evaluated" if healthy_pct is None else f"{healthy_pct:.1f}% healthy"
            lines.append(f"  {label}: {health}, {unresolved} unresolved {issue_word}")

    # Freshness line.
    if freshness_ts:
        lines.append(f"Last successful collection: {freshness_ts}")
    else:
        lines.append("Last successful collection: none")

    lines.append("")  # blank separator
    lines.append(f"Unresolved issues in total (30 days): {total_unresolved}")

    # Top-3 most-recent issues.
    if issues:
        lines.append("Most recent issues (top 3):")
        for issue in issues[:3]:
            ds = issue.get("datastream_name") or issue.get("datastream_id") or "?"
            label = _MONITOR_LABELS.get(issue.get("type", ""), issue.get("type", "?"))
            fired = (issue.get("fired_at") or "")[:10]  # YYYY-MM-DD only
            lines.append(f"  [{label}] {ds} -- {fired} (id: {issue.get('firing_id', '?')})")
    else:
        lines.append("No open issue detected.")

    # Belt-and-suspenders: clamp to 30 lines (AD-1 NFR1).
    summary = "\n".join(lines[:30])

    # ------------------------------------------------------------------
    # Build the AD-1 structuredContent envelope.
    # ------------------------------------------------------------------
    freshness_label = freshness_ts if freshness_ts else "no evaluation"
    env = _envelope(
        {
            "project_id": project_id,
            "connector": connector,
            "monitors": monitors,
            "issues": issues,
            "total_unresolved": total_unresolved,
            "evaluated_days_30d": evaluated_days,
        },
        provenance={
            "source_system": "connector-core",
            "source_field": "get_data_quality_report",
            "pull_id": None,
        },
        freshness=freshness_label,
    )

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=env,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the data-quality tool on the given FastMCP app (declared -- AD-43)."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_data_quality_report,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
