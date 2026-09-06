"""The two core-owned reporting tools: `get_daily_report` and `get_report`.

They are the LLM-agent channel of the reporting contract -- one lean text summary
plus the full AD-1 envelope on structuredContent -- and everything they compose
(warehouse query, dedup, rollup, confidence, narrative, branding) lives in the
modules they call. What lives HERE is the composition itself, plus the three
helpers only these two use: the date-range validation, the pre-query gate, and
the project's geographic posture.

`_load_project_geographic_posture` is re-exported by `core.main` because
`core.notebook_mcp` reads it from that address.

The `from core.main import ...` lines inside the bodies are the seam every
extracted surface uses: `core.main` imports this module, so a module-level import
back would be a cycle -- and `_resolve_project` and `_loaded_modules` are patched
by the suites at the `core.main` address, so capturing them at import time would
ignore the patch in silence and read production.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import branding as branding_module
from core import confidence as confidence_module
from core import dimension_lineage, narrative, summarizer, tracing, warehouse
from core import envelope as envelope_builder
from core import metrics as metrics_module
from core import rollup as rollup_module
from core.constants import AUTH_EXPIRED_CODE

logger = logging.getLogger(__name__)

#: The only date shape these tools accept on the wire.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date_range(date_range: dict | None) -> str | None:
    """Return an error message string if date_range is invalid, else None."""
    if not isinstance(date_range, dict):
        return "date_range must be a dict with 'start' and 'end' keys"
    start = date_range.get("start")
    end = date_range.get("end")
    if not start or not end:
        return "date_range must contain 'start' and 'end' keys"
    if not _ISO_DATE_RE.match(str(start)):
        return f"date_range.start must be ISO-8601 (YYYY-MM-DD), got: {start!r}"
    if not _ISO_DATE_RE.match(str(end)):
        return f"date_range.end must be ISO-8601 (YYYY-MM-DD), got: {end!r}"
    if str(start) > str(end):
        return f"date_range.start ({start}) must be ≤ date_range.end ({end})"
    return None


# ---------------------------------------------------------------------------
# Story 11.6 (AD-18) — measured pre-query gate, shared by the three data tools.
#
# The gate lives ONLY in surfaces we control (tool descriptions above + tool
# responses here); it is MEASURED, never enforced (a non-adherent query still
# returns a normal envelope). record_data_query writes the adherence verdict to
# a Langfuse trace attribute (best-effort) with a Postgres fallback; the ~1-line
# pointer nudges the agent to search_context when definitions were served (AI-50)
# but context was not consulted. AD-1: the pointer is one line and the summary
# stays <=30 lines (append_pointer_within_cap trims the tail, never the pointer).
# AD-2: source-agnostic — no module strings. Never raises into the tool path.
# ---------------------------------------------------------------------------


def _describe_detector_silence(state: str | None, readiness: object) -> str | None:
    """One line naming WHY no anomaly is reported -- or None when it needs none.

    AI-272 / CAV-13. `build_briefing` distinguishes five states (`briefing.py:557-561`)
    and a reader used to see only `anomalies_count`. Four of the five deserve a
    sentence; `no_anomaly` is the genuine all-clear and stays silent so the
    five-line briefing budget is not spent saying nothing.

    Each sentence names the fact and, where there IS one, the gesture -- an
    unarmed detector is waiting for observations, and saying how many are missing
    is what turns "nothing to report" into "not able to report yet".
    """
    if not state or state == "no_anomaly":
        return None
    if state == "anomalies_reported":
        return None
    if state == "not_supplied":
        return (
            "! Anomaly detection state unknown: readiness was not read for this "
            "briefing, so no anomaly here does not mean none."
        )
    if state == "no_series":
        return (
            "! No anomaly detection ran: no baseline series exists for this date, "
            "so nothing could be compared."
        )
    if state == "insufficient_observations":
        counts = readiness if isinstance(readiness, dict) else {}
        missing = counts.get("insufficient_count")
        minimum = counts.get("minimum_observations")
        if missing and minimum:
            return (
                f"! Anomaly detection is not armed yet: {missing} series below the "
                f"minimum of {minimum} observations. It arms itself as history accrues."
            )
        return (
            "! Anomaly detection is not armed yet: too few observations to compare "
            "against. It arms itself as history accrues."
        )
    # A state this function does not know is reported rather than swallowed: a
    # sixth value added upstream must not read as an all-clear here.
    return f"! Anomaly detection state not recognised here: {state}."


def _describe_context_events_unread(unavailable: object) -> str | None:
    """The briefing's line for a context window that was never READ -- or None.

    AI-344 / ledger context-hub[75], [78]. The briefing carries no context event
    for two very different reasons: the window was read and holds none, or no
    store could serve it. Only the first is a quiet week. The sentence is NOT
    composed here: the fork is `narrative.context_absence_line` -- the ONE
    composer every reader of context events shares since AI-350 -- and the clause
    it returns is `narrative_phrases`' ``context_unavailable``, the same the
    report's "Why" section emits for the same state. ``read_and_empty=None``
    because a briefing header spends no line saying a window was quiet: the
    counts printed beside it already say that.

    The reason and the repair ride `meta.briefing`, never the sentence -- a
    narrative line is rendered from the catalogue, never from a payload. The
    leading marker is this header's own prefix, not part of the clause.
    """
    line = narrative.context_absence_line(unavailable, read_and_empty=None)
    return f"! {line}" if line else None


def _apply_pre_query_gate(
    summary: str,
    data_tool: str,
    *,
    project_id: str,
    metric_definitions: dict | None,
    envelope: dict | None = None,
) -> tuple[str, dict | None]:
    """Record adherence + optionally append the ~1-line search_context pointer.

    Returns (summary, gate_verdict). When gate_verdict is returned, the MCP tool
    stamps the adherence verdict on ``ToolResult.meta["gate"]`` (additive key --
    observable by the widget and Epic 14 without polluting the LLM structured_content
    channel). Best-effort: any failure degrades to the original summary and None
    gate_verdict (AD-18: measuring adherence must never break a data query).
    """
    from core.main import _current_identity  # noqa: PLC0415

    try:
        from core import adherence as _adherence  # noqa: PLC0415

        # The SAME trace source as the consult mark and the Analyze tools
        # (2026-09-01): the client's traceparent as the AI Path middleware read
        # it for this call, else the active span. Reading the span alone here
        # let a client that named the exchange on every call be measured by the
        # wall-clock inference whenever tracing was off.
        trace_id = _adherence.current_exchange_trace_id()
        verdict = _adherence.record_data_query(
            data_tool,
            project_id=project_id,
            trace_id=trace_id,
            identity=_current_identity(),
        )
        gate_verdict = {
            "adherent": bool(verdict.get("adherent")),
            "context_tool": verdict.get("context_tool"),
            "session_kind": verdict.get("session_kind"),
            "adherence_basis": verdict.get("adherence_basis"),
        }
        pointer = _adherence.build_context_pointer(
            metric_definitions, adherent=bool(verdict.get("adherent"))
        )
        return _adherence.append_pointer_within_cap(summary, pointer), gate_verdict
    except Exception as _gate_exc:  # noqa: BLE001 -- gate must never break the tool
        logger.debug("%s: pre_query_gate_skipped: %s", data_tool, _gate_exc)
        return summary, None


def get_daily_report(
    project_id: str = "default",
    date_range: dict = None,
    connectors: list[str] | None = None,
    as_of: str | None = None,
) -> ToolResult:
    """Daily report -- lean LLM summary + full dataset in structuredContent.

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    CONSULT CONTEXT FIRST (AD-18): before interpreting these figures, consult the
    governed context layer -- call ``search_context`` (a metric/concept) or
    ``get_procedure`` (a named playbook). Definitions, procedures and schema docs
    live there. This is a recommendation, not a gate: the query still succeeds if
    you skip it, but adherence is measured per session (Epic 14).

    Returns a <=30-line plain-text summary on the LLM channel and the full
    canonical AD-1 envelope on structuredContent (token-burn split, AD-1 /
    NFR1 / CAP-3). Heavy JSON never enters the LLM context.

    Parameters:
        project_id: Project identifier (default: 'default')
        date_range: Dict with 'start' and 'end' ISO-8601 date strings
        connectors: Connector filter (None = all enabled connectors for project)
        as_of: Optional ISO-8601 datetime string (e.g. "2026-07-08T23:59:59Z").
               When set, returns KPI values as they were known at that timestamp
               (as-of replay — Story 4.6, FR13). When None: current view (unchanged).
    """
    # NFR1 P1 gate: target ~500 tokens LLM channel vs multi-MB widget payload.
    # NFR2: target ~30s latency.
    from core.main import (  # noqa: PLC0415  # noqa: PLC0415
        _apply_conversions_dedup,
        _enrich_envelope_with_health,
        _error,
        _fetch_context_events,
        _loaded_modules,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    t0 = time.perf_counter()
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"
    # Validate pure request inputs before any project or briefing database access.
    # T1.3 — Validate date_range
    validation_error = _validate_date_range(date_range)
    if validation_error:
        err = _error("invalid_date_range", validation_error)
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(err))],
            is_error=True,
        )

    # Story 7.1 (AC5): resolve + validate the project via the shared resolver
    # (replaces the old inline 'default' auto-bind). Archived/missing -> ToolError.
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity)

    # Story 5.1 (AC2/AC3): the per-tool root span is created by the universal
    # TracingMiddleware (tool.name / sanitised params / latency / traceparent
    # continuation). The P1 gate data (AC7) is attached to that same span below via
    # metrics.log_tool_metrics -> tracing.record_current_span_attributes.

    # Story 6.7 (AC5): fetch today's cached briefing for this project.
    # ONE SELECT from app.morning_briefings — zero warehouse calls on the hot path.
    # PERFORMANCE GUARANTEE: this is the ONLY DB call for the briefing (AC7 / hot-path guarantee).
    _briefing_row: dict | None = None
    try:
        # AC6 -- meme raison qu'aux quatre autres acquisitions de cet outil : la
        # connexion qui lit la ligne porte le contexte d'acces de l'appelant.
        from core.db import request_connection as _get_pg_for_briefing  # noqa: PLC0415

        _today_date = datetime.now(timezone.utc).date().isoformat()
        with _get_pg_for_briefing(identity) as _pg_conn_for_briefing:
            with _pg_conn_for_briefing.cursor() as _cur_briefing:
                _cur_briefing.execute(
                    """
                    SELECT id, insights, built_at
                    FROM app.morning_briefings
                    WHERE project_id = %s AND briefing_date = %s
                    """,
                    (project_id, _today_date),
                )
                _briefing_db_row = _cur_briefing.fetchone()
                if _briefing_db_row is not None:
                    _cols_briefing = [d[0] for d in _cur_briefing.description]
                    _briefing_row = dict(zip(_cols_briefing, _briefing_db_row))
    except Exception as _briefing_exc:
        logger.debug("get_daily_report: briefing_fetch_skipped: %s", _briefing_exc)

    # Story 4.6 (AC3): validate as_of if provided — must be ISO-8601 datetime.
    # as_of_ts holds the normalised string for SQL binding (timezone-aware ISO).
    as_of_ts: str | None = None
    if as_of is not None:
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        try:
            _dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            as_of_ts = _dt.isoformat()
        except (ValueError, AttributeError):
            raise ToolError(
                json.dumps(
                    {
                        "code": "invalid_input",
                        "message": "as_of must be a valid ISO-8601 datetime"
                        f" (e.g. 2026-07-08T23:59:59Z), received: {as_of!r}",
                    }
                )
            )

    # Resolve connectors: None = all enabled modules for this project (Story 7.2, AC4).
    # Filter by module enablement so disabled modules are excluded from the daily report.
    effective_connectors: list[str] | None = connectors
    if effective_connectors is None:
        from core import db as _core_db_dr  # noqa: PLC0415
        from core.module_enablement import is_module_enabled as _is_mod_enabled  # noqa: PLC0415

        try:
            with _core_db_dr.request_connection(identity) as _conn_dr:
                effective_connectors = [
                    m.name
                    for m in _loaded_modules
                    if _is_mod_enabled(m.name, project_id, _conn_dr)
                ] or None
        except Exception as _dr_exc:
            logger.debug(
                "get_daily_report: module_enablement_filter_skipped: %s", _dr_exc
            )
            effective_connectors = [m.name for m in _loaded_modules] or None

    # T2 — Query warehouse (Story 4.6: route to as-of path when as_of is set)
    try:
        if as_of_ts is not None:
            # Story 4.6 (AC3, AC4): as-of replay path.
            # Queries fact_daily_kpi_all_pulls with loaded_at <= as_of_ts.
            # Health enrichment is still applied below (connection state is current,
            # not as-of; we note this in the meta.as_of field).
            rows = warehouse.get_daily_report_asof(
                project_id,
                date_range["start"],
                date_range["end"],
                effective_connectors,
                as_of_ts,
            )
        else:
            rows = warehouse.query_daily_report(
                project_id,
                date_range["start"],
                date_range["end"],
                effective_connectors,
            )
    except Exception as exc:
        err = _error("warehouse_query_error", f"Warehouse query failed: {exc}")
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(err))],
            is_error=True,
        )

    # AD-4-conversions: dedup rule applied — see dbt/models/marts/cross_source_conversions.sql
    # Rule P: when multiple connectors are in scope, apply priority-source dedup for conversions
    # so the cross-source total is never a raw sum of overlapping source values (double-count).
    # Priority order is declarative: dbt/seeds/metric_source_priority.csv (AD-2 compliant).
    # Single-connector queries are unaffected (per-source values read directly from fact_daily_kpi).
    if effective_connectors is None or len(effective_connectors) != 1:
        rows = _apply_conversions_dedup(rows)

    # T3 — Fetch context events for the reporting window (AC7, Story 4.3).
    # AI-344 (2026-09-01): served from the mirror or the record through the
    # caller's scoped connection; when neither can serve, the report is still
    # built and `meta.context_events_unavailable` says the window was not read --
    # never "[] gracefully", which is a quiet window the reader cannot doubt.
    # AD-2: _fetch_context_events is generic; no module-specific strings.
    from core.context_events import (  # noqa: PLC0415
        ContextEventsUnavailable as _ContextEventsUnavailable,
    )

    context_events_unavailable: dict | None = None
    try:
        context_events = _fetch_context_events(
            project_id, date_range["start"], date_range["end"], identity=identity
        )
    except _ContextEventsUnavailable as _cev_exc:
        context_events = []
        context_events_unavailable = _cev_exc.payload

    # T3 — Build LLM summary (≤30 lines, from rollup aggregates — never raw rows)
    # cost values from fact_daily_kpi are always in canonical_currency (EUR by default).
    # AD-6: normalization happened once, at dbt staging. No conversion here.
    connector_list = effective_connectors or []

    # G-06 fix: query_daily_report now fetches current + prior window rows so that
    # rollup._split_periods can compute delta/delta_pct. Split here so the rollup
    # gets the full set (for delta), while envelope/summary only see current rows.
    _current_rows, _prior_rows = rollup_module._split_periods(
        rows, date_range["start"], date_range["end"]
    )
    # Use current_rows everywhere data.rows / summary tables are built (below),
    # but pass all rows to compute_rollup so _split_periods inside can find prior.
    # Note: compute_rollup calls _split_periods internally, so passing all rows
    # gives it both buckets; replace `rows` with current_rows for envelope data.
    rows = _current_rows

    # Story 6.4 (AC9): compute the canonical rollup dict (totals + deltas vs the
    # previous period, with provenance) via the shared rollup helper. This is the
    # input to the deterministic what+why narrative builder (AD-1). Metrics and
    # pull_ids are derived from the returned rows (source-agnostic, AD-2).
    _rollup_rows = _current_rows + _prior_rows
    rollup_metrics = sorted({r.get("metric") for r in _rollup_rows if r.get("metric")})
    rollup_pull_ids = sorted({r.get("pull_id") for r in _rollup_rows if r.get("pull_id")})
    # CAV-02 (story 53.2): ask the reconciliation gate whether several sources may be
    # added at all. This is THE daily report -- the busiest producer of cross-source
    # totals in the product -- and it was one of the five call sites that never asked
    # while the register recorded the gate as wired from the card path alone.
    from core.metric_reconciliation import route_status_resolver  # noqa: PLC0415

    rollup = rollup_module.compute_rollup(
        _current_rows + _prior_rows,
        rollup_metrics,
        date_range["start"],
        date_range["end"],
        project_id,
        rollup_pull_ids,
        route_resolver=route_status_resolver(project_id),
    )

    # Story 6.4 (AC5, AC10): the LLM summary is the deterministic what+why narrative
    # with inline citations (build_narrative), replacing the legacy rollup summary.
    # It is assembled from the rollup dict + context events only — never raw rows
    # (AD-1). Alert firings are appended to the text channel further below (the
    # existing business/anomaly append path), so alerts=[] is passed here.
    # When there are no rows (mart not populated), fall back to the legacy empty-state
    # summarizer so the empty-state French messaging + as-of caveat are preserved.
    if rows:
        # THE THIRD PROACTIVE PATH, scoped 2026-08-30 (migration 322).
        # `_fetch_context_events` returns every annotation of the window, and
        # this call handed the whole list to the "Why" section under every claim
        # of the report -- an attachment on no basis but co-occurrence in a date
        # range, which `proactive-assertions.md` forbids and which the briefing
        # and the anomaly evaluator had already stopped doing. The same rule, the
        # same vocabulary, one function: an event naming another metric or
        # another connector than this report's is out, one naming neither stays,
        # and the descriptor travels with the section so the reader sees what was
        # compared and what could not be.
        from core.briefing import (  # noqa: PLC0415
            context_events_in_claim_scope as _scope_events,
        )

        _scoped_events, _context_scope = _scope_events(
            context_events,
            start=date_range["start"],
            end=date_range["end"],
            metrics=rollup_metrics,
            connectors=connector_list,
        )
        summary = narrative.build_narrative(
            project_id=project_id,
            report_id=None,
            rollup=rollup,
            context_events=_scoped_events,
            alerts=[],
            as_of=as_of_ts,
            narrative_prompt=None,
            context_scope=_context_scope,
            context_unavailable=context_events_unavailable,
        )
    else:
        # AI-350: the marker travels into the zero-row branch too. Until it did,
        # this was the ONE reader of context events with no channel for "not
        # read": a window whose events could not be served was described as
        # "aucun événement connu" on the model channel while
        # `meta.context_events_unavailable` said the opposite two keys away.
        summary = summarizer.build_daily_report_summary(
            rows, date_range, connector_list, project_id,
            context_events=context_events, as_of=as_of_ts,
            context_events_unavailable=context_events_unavailable,
        )

    # T4 — Build canonical envelope (structuredContent channel)
    meta_freshness, meta_provenance = envelope_builder.derive_meta_from_rows(
        rows, connector_list
    )
    envelope = envelope_builder.build_canonical_envelope(
        rows=rows,
        meta_freshness=meta_freshness,
        meta_provenance=meta_provenance,
        date_range=date_range,
        connectors=connector_list,
        report_profile="standard_daily",
        # Story 23.1 (AC1): org branding of the project's organization, additive
        # meta key (AI-31). None (no org / no colors / DB error) -> key omitted,
        # the widget shell renders the default toorow theme.
        branding=branding_module.resolve_org_branding(project_id),
        # Story 27.9 (the reading path): the client's own name for each
        # dimension this report actually shows, resolved through the cascade
        # PROJECT > ORG > PLATFORM. A dimension with no client label falls back
        # to its stable identifier and SAYS so, so a reader can tell a chosen
        # name from a default.
        dimension_labels=dimension_lineage.resolve_report_dimension_labels(
            project_id, rows
        ),
    )
    # AD-14: inject resolved identity into the envelope data (Story 2.3)
    envelope["data"]["identity"] = identity

    # Story 5.3 (AC7): add recent business threshold alert firings to meta.alerts[].
    # Additive -- appends to existing alerts[] (infra alerts already use this field).
    # Best-effort: DB errors return [] silently (graceful degradation).
    _business_alert_firings: list[dict] = []
    try:
        from core import business_alerts as _business_alerts_module  # noqa: PLC0415
        from core.db import request_connection as _get_pg_conn  # noqa: PLC0415

        with _get_pg_conn(identity) as _pg_conn_for_alerts:
            _business_alert_firings = _business_alerts_module.fetch_recent_alert_firings(
                project_id, _pg_conn_for_alerts, hours=24
            )
            # AI-32 (Story 6.1): meta_alert scheduler-health rows share the alert
            # delivery path -- surface them in meta.alerts[] the next morning.
            _business_alert_firings = _business_alert_firings + (
                _business_alerts_module.fetch_recent_meta_alerts(
                    project_id, _pg_conn_for_alerts, hours=24
                )
            )
    except Exception as _ba_exc:
        logger.debug(
            "get_daily_report: business_alerts_fetch_skipped: %s", _ba_exc
        )

    # Story 5.4 (AC8): add recent anomaly firings to meta.alerts[].
    # type='anomaly' rows from app.alert_firings, fetched alongside business threshold rows.
    # The fetch_recent_alert_firings in business_alerts filters type='business_threshold';
    # anomaly firings are fetched separately via anomaly_alerts.fetch_recent_anomaly_firings.
    # Both types surface in meta.alerts[] (AC8: both channels wired).
    _anomaly_firings: list[dict] = []
    try:
        from core import anomaly_alerts as _anomaly_alerts_module  # noqa: PLC0415
        from core.db import request_connection as _get_pg_conn_anon  # noqa: PLC0415

        with _get_pg_conn_anon(identity) as _pg_conn_for_anomalies:
            _anomaly_firings = _anomaly_alerts_module.fetch_recent_anomaly_firings(
                project_id, _pg_conn_for_anomalies, hours=24
            )
    except Exception as _an_exc:
        logger.debug(
            "get_daily_report: anomaly_alerts_fetch_skipped: %s", _an_exc
        )

    # Story 22.6 (review F-2): media-plan pacing firings surface in the SAME live
    # meta.alerts[] channel as business/anomaly ones -- not only in the briefing.
    _mediaplan_firings: list[dict] = []
    try:
        from core import mediaplan_alerts as _mediaplan_alerts_module  # noqa: PLC0415
        from core.db import request_connection as _get_pg_conn_mp  # noqa: PLC0415

        with _get_pg_conn_mp(identity) as _pg_conn_for_mediaplan:
            _mediaplan_firings = _mediaplan_alerts_module.fetch_recent_mediaplan_firings(
                project_id, _pg_conn_for_mediaplan, hours=24
            )
    except Exception as _mp_exc:
        logger.debug(
            "get_daily_report: mediaplan_alerts_fetch_skipped: %s", _mp_exc
        )

    # Append ALL alert types to existing meta.alerts[] (created by envelope builder).
    _all_alert_firings = _business_alert_firings + _anomaly_firings + _mediaplan_firings
    if _all_alert_firings:
        existing_alerts = envelope.get("meta", {}).get("alerts", [])
        envelope.setdefault("meta", {})["alerts"] = existing_alerts + _all_alert_firings

    # Story 4.3 (AC7): add context_events to meta as an additive key.
    # Story 31.5: extended with platform/source/value (MMM regressors) + category/default_marker
    # (dim_event_type join via enrich_events_with_dim) so the overlay widget can style markers
    # by type and filter by category/platform. Additive — schema_version stays "1".
    from core.report_dictionary import enrich_events_with_dim as _enrich  # noqa: PLC0415
    _enriched_events = _enrich(context_events)
    envelope.setdefault("meta", {})["context_events"] = [
        {
            "id": e.get("id"),
            "event_date": e.get("event_date"),
            "type": e.get("type"),
            "label": e.get("label"),
            "platform": e.get("platform"),
            "source": e.get("source"),
            "value": e.get("value"),
            "category": e.get("category", ""),
            "default_marker": e.get("default_marker", "pin"),
        }
        for e in _enriched_events
    ]
    if context_events_unavailable is not None:
        envelope["meta"]["context_events_unavailable"] = context_events_unavailable

    # Story 4.6 (AC5): add meta.as_of when as-of replay is active.
    # Null when not set (current view) — explicit null so widget can test for it.
    envelope.setdefault("meta", {})["as_of"] = as_of_ts

    # Story 5.5 (AC2): expose current OTel trace_id in meta so the widget can
    # echo it back in submit_feedback (AD-13 — Postgres audit + Langfuse score on
    # the same trace). When TRACING_ENABLED=false, current_trace_id_hex() returns
    # None — meta carries "trace_id": null so feedback still works (trace row is
    # written with trace_id=NULL; Langfuse score is skipped). Additive key.
    envelope.setdefault("meta", {})["trace_id"] = tracing.current_trace_id_hex() or None

    # Story 7.1 (AC7): echo the resolved project_id in meta so the widget shell
    # can scope callServerTool (e.g. submit_feedback) to the correct project.
    # Additive key (AI-31 policy).
    envelope.setdefault("meta", {})["project_id"] = project_id

    # Story 2.5 (AC5, AC6, AC7, AC8): enrich envelope with connection health.
    # No-op when DB is unreachable or no connection this project pulled through
    # over this window exists (Epic 1 backward compat).
    #
    # Story 53.3, second pass: the window travels with the call. It used to be
    # absent from the signature AND from the query, so the health badge on a
    # January report could come from a connection that only ever served July.
    envelope = _enrich_envelope_with_health(
        envelope,
        project_id,
        date_from=date_range.get("start"),
        date_to=date_range.get("end"),
    )

    # Story 3.5 (AC8) + Story 4.2 (AC8 / AI-21 fix): inject completeness confidence
    # from pull_verifications, scoped per connection_ref_id for the requested connectors.
    # AI-27 (Story 5.1): the lookup now lives in core.confidence (behaviour-preserving).
    # Best-effort: DB errors silently produce confidence=None (no confidence key in meta).
    #
    # `stale_since` is handed over deliberately: these two calls answer the SAME
    # question two lines apart, and until story 53.3's second pass they could
    # answer it in opposite directions in one payload (stale_since set, freshness
    # 1.0). The health verdict now caps the freshness term (`core.confidence`).
    confidence = confidence_module.compute_confidence(
        project_id,
        effective_connectors,
        rows=rows,
        date_from=date_range.get("start"),
        date_to=date_range.get("end"),
        stale_since=(
            (envelope.get("meta") or {}).get("freshness") or {}
        ).get("stale_since"),
    )
    if confidence is not None:
        envelope.setdefault("meta", {})["confidence"] = confidence

    # Story 6.7 (AC5, AC6): inject briefing into summary and meta.
    # If a cached briefing exists for today: prepend "Briefing matinal" section (≤5 lines)
    # to the summary, and set meta.briefing. Otherwise meta.briefing = null.
    _briefing_insights: list[dict] = []
    _briefing_meta: dict | None = None

    if _briefing_row is not None:
        _raw_insights = _briefing_row.get("insights") or {}
        _briefing_insights = (
            _raw_insights.get("insights", []) if isinstance(_raw_insights, dict) else []
        )
        _briefing_date_str = (
            _raw_insights.get("briefing_date", _today_date)
            if isinstance(_raw_insights, dict)
            else _today_date
        )
        _built_at = _briefing_row.get("built_at")
        _built_at_str = (
            _built_at.isoformat() if hasattr(_built_at, "isoformat") else str(_built_at or "")
        )

        # Build briefing summary header. The budget was AC5's ≤5 lines (1 header +
        # top 3 insights + 1 blank) and becomes ≤6: the detector-silence line below
        # is added rather than taken out of the three insights, because a specific
        # finding is not worth trading for a note about the absence of findings.
        # The constraint that is actually TESTED -- NFR1's 30-line cap -- is
        # untouched: the truncation below reserves from `len(_brief_lines)`.
        # NOTE the French header, and it is not this change's business: it is pinned
        # by four assertions (`test_gap_fixes_core.py:418,435,445,451`,
        # `test_get_daily_report.py:882`) and is carried as language debt.
        _icons = {"business_alert": "⚠", "anomaly": "⚠", "notable_delta": "✓"}
        _brief_lines = [f"[Briefing matinal — {_briefing_date_str}]"]
        for _ins in _briefing_insights[:3]:
            _icon = _icons.get(_ins.get("type", ""), "•")
            _headline = _ins.get("headline", "")
            _citation = _ins.get("citation", "")
            _brief_lines.append(f"{_icon} {_headline} {_citation}")

        # AI-272 / CAV-13 — A SILENT DETECTOR IS NOT AN ALL-CLEAR, and this is the
        # only place a reader can be told the difference. `build_briefing` has
        # produced `anomaly_state` since story 53.8, with five disjoint values, and
        # this block dropped it: `meta.briefing` carried date, insights and
        # built_at, so "no anomaly fired" and "the detector could not fire at all"
        # arrived identical. Which is exactly the belief CAV-13 describes.
        #
        # The line goes in the SUMMARY and not only in `meta`, because the summary
        # is what the assistant reads. A state carried in a structured field no
        # reader consults would be the same defect one level down.
        #
        # `no_anomaly` adds no line: THAT one is the all-clear, and saying it would
        # spend a line of a five-line budget to say nothing.
        _anomaly_state = (
            _raw_insights.get("anomaly_state") if isinstance(_raw_insights, dict) else None
        )
        _readiness = (
            _raw_insights.get("detector_readiness") if isinstance(_raw_insights, dict) else None
        )
        _silence = _describe_detector_silence(_anomaly_state, _readiness)
        if _silence:
            _brief_lines.append(_silence)

        # AI-344 -- same reasoning one notch over: an empty briefing whose context
        # window was never read is not a quiet week, and the summary is the only
        # channel the assistant reads.
        _briefing_context_unavailable = (
            _raw_insights.get("context_events_unavailable")
            if isinstance(_raw_insights, dict)
            else None
        )
        _context_unread = _describe_context_events_unread(_briefing_context_unavailable)
        if _context_unread:
            _brief_lines.append(_context_unread)

        _brief_lines.append("")  # blank separator

        # Prepend briefing section to summary (briefing uses ≤5 lines of the 30-line cap)
        _brief_section = "\n".join(_brief_lines)
        _combined = _brief_section + "\n" + summary
        # NFR1 re-enforcement: prepend may push total past 30 lines. Trim the
        # summary tail (not the briefing block) to keep the cap. The briefing
        # header occupies the first len(_brief_lines) lines; everything after that
        # is the former summary -- truncate it so total stays <= 30 lines.
        # _MAX_LINES is defined below at the alert-append section; use literal 30 here.
        _nfr1_cap = 30
        _combined_lines = _combined.split("\n")
        if len(_combined_lines) > _nfr1_cap:
            _brief_line_count = len(_brief_lines)
            # Reserve one line for the truncation marker so the total stays at the cap.
            _allowed_summary_lines = _nfr1_cap - _brief_line_count - 1
            if _allowed_summary_lines > 0:
                _combined_lines = (
                    _combined_lines[:_brief_line_count]
                    + _combined_lines[
                        _brief_line_count : _brief_line_count + _allowed_summary_lines
                    ]
                    + ["[tronque]"]
                )
            else:
                _combined_lines = _combined_lines[:_nfr1_cap]
        summary = "\n".join(_combined_lines)

        # Build meta.briefing (AC6)
        _briefing_meta = {
            "briefing_date": _briefing_date_str,
            "insights": _briefing_insights,
            "built_at": _built_at_str,
            # AI-272: the five states travel WITH the count they qualify. A
            # consumer reading `anomalies_count == 0` off the insights list can
            # now ask why, instead of assuming.
            "anomaly_state": _anomaly_state,
            "detector_readiness": _readiness,
        }
        # AI-344: the reason and the repair travel structured, beside the
        # sentence -- the same key, with the same shape, this envelope already
        # carries under `meta.context_events_unavailable` for its OWN window.
        if _briefing_context_unavailable:
            _briefing_meta["context_events_unavailable"] = _briefing_context_unavailable

    # meta.briefing = null when no briefing (AC6: explicit null for backward compat)
    envelope.setdefault("meta", {})["briefing"] = _briefing_meta

    # Story 2.5 AC8(a): if auth_expired alert was injected, append to LLM summary.
    # This keeps the text channel honest without exposing structured JSON to the LLM.
    _health_alerts = envelope.get("meta", {}).get("alerts", [])
    if any(a.get("code") == AUTH_EXPIRED_CODE for a in _health_alerts):
        summary = (
            summary.rstrip()
            + "\n\nToken de connexion expire -- donnees indisponibles."
        )

    # Story 5.3 (AC7, LLM channel): append one-liner per business alert firing.
    # Story 5.4 (AC8, LLM channel): append one-liner per anomaly firing after business alerts.
    # NFR1: enforce ≤30 lines total -- if many firings, truncate to fit.
    _current_lines = summary.count("\n") + 1
    _MAX_LINES = 30

    if _business_alert_firings:
        from core import business_alerts as _ba_llm  # noqa: PLC0415

        for _firing in _business_alert_firings:
            if _current_lines >= _MAX_LINES:
                break
            _alert_line = _ba_llm.format_alert_line(_firing)
            summary = summary.rstrip() + "\n" + _alert_line
            _current_lines += 1

    if _anomaly_firings:
        from core import anomaly_alerts as _an_llm  # noqa: PLC0415

        for _an_firing in _anomaly_firings:
            if _current_lines >= _MAX_LINES:
                break
            _anomaly_line = _an_llm.format_anomaly_line(_an_firing)
            summary = summary.rstrip() + "\n" + _anomaly_line
            _current_lines += 1

    # AI-50: inject metric_definitions (+ llm_commentary_guidelines if present) from
    # the module pack's DEFAULT report doc, exactly as get_card does on the ad-hoc path
    # via cards._fetch_r6_adhoc. Best-effort: no DB / no pack -> omit cleanly.
    # The injected keys go on envelope["data"] so the widget can consume them alongside
    # the rows -- same placement as get_report (story 8.8 R6) and get_card.
    try:
        from core import cards as _cards_module  # noqa: PLC0415

        _dr_metrics = sorted({r.get("metric") for r in rows if r.get("metric")})
        _r6_metric_defs, _r6_guidelines = _cards_module._fetch_r6_adhoc(
            project_id, _dr_metrics, list(_loaded_modules)
        )
        if _r6_metric_defs is not None:
            envelope.setdefault("data", {})["metric_definitions"] = _r6_metric_defs
        if _r6_guidelines is not None:
            envelope.setdefault("data", {})["llm_commentary_guidelines"] = _r6_guidelines
    except Exception as _r6_exc:
        logger.debug("get_daily_report: r6_metric_defs_skipped: %s", _r6_exc)

    # Story 11.6 (AD-18): measure the pre-query gate (never enforce it). Record
    # whether a context call preceded this data query in the same session, and --
    # when definitions are served (AI-50) but context was NOT consulted -- add a
    # ONE-line pointer to search_context (AD-1: ~1 line, never a dump). The <=30-line
    # summary cap is preserved (adherence.append_pointer_within_cap).
    summary, gate_verdict = _apply_pre_query_gate(
        summary,
        "get_daily_report",
        project_id=project_id,
        metric_definitions=envelope.get("data", {}).get("metric_definitions"),
        envelope=envelope,
    )

    # T5 — Measure and log metrics (P1 gate evidence)
    # Story 5.1 (AC2/AC7): the same P1 gate figures (summary_token_estimate,
    # payload_bytes, latency_ms) are attached to the tool span so the token-burn
    # split is verifiable per call in Langfuse — no duplicated measurement logic.
    latency_ms = int((time.perf_counter() - t0) * 1000)
    payload_bytes = len(json.dumps(envelope).encode("utf-8"))
    metrics_module.log_tool_metrics(
        "get_daily_report", summary, payload_bytes, latency_ms
    )

    # Story 50.6 -- THE WIDGET BINDING IS GONE, both of them.
    #
    # This tool used to carry the resource TWICE: a result-level
    # `_meta = {"ui": {"resourceUri": DAILY_REPORT_WIDGET_URI}}` here, and a
    # declaration-level `AppConfig(resource_uri=...)` on the registration below.
    # `visualization-and-rendering.md` ("Tool split") says a data tool does not
    # attach a widget resource and only the render tool advertises one, and
    # `mcp_profiles.assert_data_render_split` now aborts boot if any tool other than
    # `render_analyze_result` does. The `ui://core/*` resource REGISTRATIONS stay:
    # a resource no data tool advertises is inert, and deleting them would destroy
    # the inventory of what Story 50.5 replaces.
    #
    # And the dataset is gone from the model channel -- but NOT by a call written
    # here. `mcp_profiles.enforce_result_model_channel` runs the split and the
    # refusal on EVERY returning tool result from the `on_call_tool` hook, so this
    # tool is bounded by the catalog-wide guard rather than by its own discipline.
    # This site used to carry that pair by hand, which is how three other tools in
    # this file shipped over budget with nobody noticing (AC4: "one shared
    # server-side guard rather than each tool's discipline").

    # T6 — Return both channels per AD-1 dual-channel pattern
    meta = {}
    if gate_verdict is not None:
        meta["gate"] = gate_verdict

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
        meta=meta if meta else None,
    )






def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind `get_daily_report` on the given FastMCP app.

    `get_report` is registered by `core.report_mcp`, where it is DEFINED. Binding
    it from here instead would have kept one call site for the surface at the
    price of a registration the scope guard cannot resolve to a function -- and
    `test_no_registration_escapes_the_scan` is right to refuse that: a tool it
    cannot resolve is a tool counted nowhere.

    Declared (AD-43) as an Insights read.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_daily_report,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
