"""Story 6.1 -- `get_report`, the named connector report.

The LLM-agent channel of the report contract: it parses the arguments, enriches
the meta with context events and alerts, and returns the dual-channel ToolResult
(AD-1). Rendering itself lives in `core.reports`.

It travels with `_load_project_geographic_posture`, whose only caller it is --
`core.notebook_mcp` reads that helper through `core.main`, which re-exports it.

`get_daily_report` stayed in `core.reporting_mcp` and is a different question:
one is the signature daily report of a Project, the other is a named report of a
Connector. They shared a file because they were written the same week, not
because they answer together -- and together they were 1 055 lines, over the
thousand-line ceiling `CLAUDE.md` sets.

The `from core.main import ...` lines inside the body are the seam every
extracted surface uses: `core.main` imports this module, so a module-level
import back would be a cycle, and the helpers are patched at the `core.main`
address.
"""

from __future__ import annotations

import json
import logging
import time

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import metrics as metrics_module
from core import tracing
from core.reporting_mcp import _apply_pre_query_gate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core-owned cross-connector tool: get_report (AD-2, Story 6.1, AC3).
#
# Renders a named, expert-designed report from a module's reports/ pack. This is
# a SEPARATE tool from get_daily_report (design decision, Dev Notes): get_report
# serves fixed metric/dimension report definitions with a narrative_prompt, while
# get_daily_report serves ad-hoc multi-connector KPI queries. Keeping them
# separate keeps each tool single-responsibility (AD-2).
#
# The rendering logic lives in core.reports (main.py stays lean — the thin tool
# def here parses args, enriches meta with context_events/alerts, and returns the
# dual-channel ToolResult).
# ---------------------------------------------------------------------------


def _load_project_geographic_posture(project_id: str, identity: str):
    """Best-effort posture + live coverage for every named-report execution path.

    Story 37.8: the posture carries the client-defined MARKETS (stable id,
    label, member country codes), and ``render_report`` projects them into
    ``data.geography.markets`` / ``data.geography.buckets`` so every tool path
    exposes the market split rather than raw ISO codes. A posture read failure
    degrades to Global + an honest ``unavailable`` coverage, never to a silent
    consolidated answer presented as a market split.

    ``identity`` is REQUIRED and is not decoration (67-1, 2026-08-24). This
    helper reads the governed geography of a Project, and it did so on a bare
    ``get_connection()`` -- so the caller's access context stopped at the tool
    boundary while the market split was read underneath it. Its single
    production caller (``get_report``) has resolved the identity twelve lines
    above; the parameter is what carries it down instead of defaulting to
    ``None``, which would be a hole that reads like a repair.
    """

    from core import db as _core_db  # noqa: PLC0415

    # Story 37.9: the GOVERNED geography, not `project_preferences`. This read used
    # `fetch_project_geographic_posture`, and the posture it produced was then handed
    # to a `render_report` argument Story 48.2 had already made a no-op -- so it
    # selected nothing, and the COVERAGE derived beside it still described a governed
    # Project as consolidated. Dead weight that answered wrongly.
    from core.country_activation import governed_posture  # noqa: PLC0415
    from core.geographic_reporting import (  # noqa: PLC0415
        GeographicPosture,
        GeographicReportContext,
        fetch_project_geographic_coverage,
    )

    posture = GeographicPosture()
    coverage: dict[str, object] = {"status": "consolidated", "datastreams": []}
    try:
        with _core_db.request_connection(identity) as conn:
            posture = governed_posture(conn, project_id=project_id)
            try:
                coverage = fetch_project_geographic_coverage(project_id, posture, conn)
            except Exception as exc:
                logger.debug(
                    "report geographic coverage read skipped project=%s: %s", project_id, exc
                )
                coverage = {
                    "status": "unavailable" if posture.mode == "local_markets" else "consolidated",
                    "datastreams": [],
                }
    except Exception as exc:
        logger.debug("report geographic posture read skipped project=%s: %s", project_id, exc)
    return GeographicReportContext(posture=posture, coverage=coverage)

def get_report(
    project_id: str,
    report_id: str,
    date_from: str = "",
    date_to: str = "",
) -> ToolResult:
    """Named expert report -- lean LLM summary + full dataset in structuredContent.

    # AD-14: identity resolved from OAuth 2.1 + PKCE (Story 2.3)

    CONSULT CONTEXT FIRST (AD-18): before interpreting this report, consult the
    governed context layer -- ``search_context`` (a metric/concept) or
    ``get_procedure`` (a named playbook). Not a gate: the query still succeeds if
    you skip it, but adherence is measured per session (Epic 14).

    Renders a connector-shipped report definition ("{module_name}/{report_def_id}")
    from warehouse data alone (FR6). The report's narrative_prompt sets the expert
    domain 'voice' prefixed to the ≤30-line LLM summary (AD-1); the full dataset +
    provenance rides on the canonical envelope for the widget channel.

    Non-additive metrics (declared aggregation_rule in the dbt dictionary) route
    to their semantic view rather than being SUM()'d (AD-4).

    Parameters:
        project_id: Project identifier.
        report_id:  "{module_name}/{report_def_id}".
        date_from:  ISO-8601 start; empty -> today - date_window.default_days.
        date_to:    ISO-8601 end; empty -> today.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as _core_db  # noqa: PLC0415
    from core import reports as reports_module  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _fetch_context_events,  # noqa: PLC0415
        _loaded_modules,
        _resolve_project,
        get_access_token,
    )
    from core.module_enablement import is_module_enabled  # noqa: PLC0415

    t0 = time.perf_counter()
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id, identity)

    # Story 7.2 (AC4): check module enablement before rendering.
    # report_id format: "{module_name}/{report_def_id}"
    _report_module_name = report_id.split("/")[0] if "/" in report_id else None
    if _report_module_name and any(
        m.name == _report_module_name for m in _loaded_modules
    ):
        # (Audit 2026-08-20, C20) The check runs only for a KNOWN module: for an
        # unknown one the downstream lookup must answer `not_found` -- the
        # accurate diagnosis -- instead of an enablement error that masks it.
        try:
            with _core_db.request_connection(identity) as _conn_mod:
                if not is_module_enabled(_report_module_name, project_id, _conn_mod):
                    raise ToolError(
                        json.dumps(
                            {
                                "code": "module_disabled",
                                "message": (
                                    f"Module {_report_module_name} is not enabled"
                                    " for this project."
                                ),
                            }
                        )
                    )
        except ToolError:
            raise
        except Exception as _me_exc:
            # Db-less seams are a SUPPORTED mode (the pre-query-gate integration
            # tests run get_report with no Postgres at all), so this path cannot
            # refuse. What it must not be is SILENT (it was a debug line): the
            # unverified state is named at WARNING, and the report renders.
            logger.warning(
                "get_report: module_enablement_unverified: %s: %s",
                type(_me_exc).__name__,
                _me_exc,
            )

    trace_id = tracing.current_trace_id_hex() or None

    # R6 (Epic 8): resolve the merged report doc (base pack + project override)
    # to extract metric_definitions (widget tooltips/direction-tint) and
    # llm_commentary_guidelines (narrative grounding). Best-effort: DB failure
    # degrades gracefully (no definitions — widget and narrative unchanged).
    _r6_metric_defs: dict | None = None
    _r6_guidelines: str | None = None
    _merged: dict | None = None  # Story 9.8: also carries the preferred card_template.
    try:
        from core import flows as _flows_module  # noqa: PLC0415

        _base_doc = _flows_module._base_report_doc(report_id, _loaded_modules)
        with _core_db.request_connection(identity) as _conn_r6:
            _override = _flows_module._fetch_report_override(project_id, report_id, _conn_r6)
        _merged = _flows_module._merge_report(_base_doc, _override, report_id, project_id)
        _r6_metric_defs = _merged.get("metric_definitions") or None
        _r6_guidelines = (_merged.get("llm_commentary_guidelines") or "").strip() or None
    except Exception as _r6_exc:
        logger.debug("get_report: r6_override_fetch_skipped: %s", _r6_exc)

    try:
        summary, envelope, widget_uri = reports_module.render_report(
            _loaded_modules,
            project_id,
            report_id,
            date_from,
            date_to,
            trace_id=trace_id,
            metric_definitions=_r6_metric_defs,
            llm_commentary_guidelines=_r6_guidelines,
            geographic_posture=_load_project_geographic_posture(project_id, identity),
            identity=identity,
        )
    except reports_module.ReportNotFound:
        raise ToolError(
            json.dumps(
                {"code": "not_found", "message": f"Report not found: {report_id}"}
            )
        )
    except Exception as exc:
        raise ToolError(
            json.dumps(
                {"code": "warehouse_query_error", "message": f"Report render failed: {exc}"}
            )
        )

    # AD-14: inject resolved identity into the envelope data.
    envelope["data"]["identity"] = identity

    # Enrich meta.context_events for the report window. AI-344 (2026-09-01): read
    # from the mirror or the record through the caller's scoped connection; when
    # neither can serve, the report still lands and `meta` says the events were
    # not read -- never "[] on DB down", which reads as a quiet window.
    # Story 31.5: expose platform/source/value + dim_event_type join (category/default_marker).
    from core.context_events import (  # noqa: PLC0415
        ContextEventsUnavailable as _ContextEventsUnavailable,
    )

    start = envelope["data"]["date_range"]["start"]
    end = envelope["data"]["date_range"]["end"]
    try:
        context_events = _fetch_context_events(project_id, start, end, identity=identity)
    except _ContextEventsUnavailable as _cev_exc:
        context_events = []
        envelope.setdefault("meta", {})["context_events_unavailable"] = _cev_exc.payload
    from core.report_dictionary import enrich_events_with_dim as _enrich2  # noqa: PLC0415
    _enriched2 = _enrich2(context_events)
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
        for e in _enriched2
    ]

    # Append recent alert firings (business + anomaly + meta_alert) to meta.alerts[].
    # Same graceful-degradation pattern as get_daily_report (best-effort).
    _firings: list[dict] = []
    try:
        from core import business_alerts as _ba  # noqa: PLC0415

        # 67-1 (2026-08-24): this site was ALIASED (`from core.db import
        # get_connection as _pg`) and the AST sweep of
        # `test_mcp_surfaces_acquire_an_armed_connection.py` matched only the
        # call name -- so a bare acquisition hid behind two characters. The
        # sweep now resolves import aliases; this line is armed like the rest.
        with _core_db.request_connection(identity) as _conn:
            _firings = _ba.fetch_recent_alert_firings(project_id, _conn, hours=24)
            _firings = _firings + _ba.fetch_recent_meta_alerts(project_id, _conn, hours=24)
    except Exception as _exc:  # noqa: BLE001
        logger.debug("get_report: alerts_fetch_skipped: %s", _exc)
    if _firings:
        existing = envelope.get("meta", {}).get("alerts", [])
        envelope.setdefault("meta", {})["alerts"] = existing + _firings

    # Story 9.8: honor the flow.report preferred card_template. When the merged report
    # doc pins a card_template, serve that card (its widget + card-shaped envelope) via
    # the SAME card path get_card uses. Best-effort: any failure keeps the default report
    # widget/envelope (never breaks the report). An unknown template id is handled inside
    # cards.get_card (structured warning + falls back to the report's default rendering).
    _card_template = ""
    if isinstance(_merged, dict):
        _card_template = (_merged.get("card_template") or "").strip()
    if _card_template:
        try:
            from core import cards as _cards  # noqa: PLC0415

            if _cards.get_template(_card_template) is not None:
                c_summary, c_envelope, c_widget_uri = _cards.get_card(
                    _loaded_modules,
                    project_id,
                    template=_card_template,
                    report_ref=report_id,
                    date_from=date_from,
                    date_to=date_to,
                    identity=identity,
                    context_events=context_events,
                    alerts=_firings,
                    trace_id=trace_id,
                )
                c_envelope["data"]["identity"] = identity
                summary, envelope, widget_uri = c_summary, c_envelope, c_widget_uri
            else:
                logger.warning(
                    "get_report: flow_card_template_ignored report=%s unknown_template=%s",
                    report_id,
                    _card_template,
                )
        except Exception as _card_exc:  # noqa: BLE001 -- never break the report
            logger.warning(
                "get_report: card_template_render_failed report=%s template=%s: %s",
                report_id,
                _card_template,
                _card_exc,
            )

    # Story 45.1: expose the exact governed business routes used by this view.
    # Report-chain inheritance remains derived evidence; AD-9 provenance is untouched.
    try:
        from core import business_taxonomy as _business_taxonomy  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        with _core_db.request_connection(identity) as _business_conn:
            _business_access = resolve_strict_resource_access(
                identity,
                _business_conn,
                project_id=project_id,
                minimum_capability="view",
                hold_access=True,
            )
            if _business_access.allowed and _business_access.org_id:
                _business_paths = _business_taxonomy.resolve_report_paths(
                    _business_conn,
                    org_id=_business_access.org_id,
                    project_id=project_id,
                    report_id=report_id,
                    actor=identity,
                    trace_id=trace_id,
                    loaded_modules=_loaded_modules,
                )
                _business_conn.commit()
                envelope.setdefault("meta", {})["business_context_paths"] = _business_paths
                envelope["meta"]["business_context_state"] = "resolved"
                tracing.record_current_span_attributes(
                    {
                        "context.business_path_count": len(_business_paths),
                        "context.business_path_keys": ",".join(
                            path["path_key"] for path in _business_paths
                        ),
                    }
                )
            else:
                # Denied is not "no governed route": say which one it is, or the
                # evaluation scores an access refusal as a missing path.
                envelope.setdefault("meta", {})["business_context_paths"] = []
                envelope["meta"]["business_context_state"] = "denied"
    except Exception as _business_exc:  # noqa: BLE001 -- report rendering stays available
        logger.debug(
            "get_report: business_path_resolution_skipped: %s",
            type(_business_exc).__name__,
        )
        # An empty list is a FACT ("this view has no governed route"). A failed
        # resolution is not that fact, and scripts/run_evals.py reads this exact
        # key as the observed evidence -- so the third state is explicit and the
        # eval maps it to `unverifiable` rather than to a verdict.
        envelope.setdefault("meta", {})["business_context_paths"] = []
        envelope["meta"]["business_context_state"] = "unavailable"
    # Story 11.6 (AD-18): measure the pre-query gate + optional search_context
    # pointer (never enforce). metric_definitions on the envelope come from the R6
    # override (AI-50); when present and context was not consulted, the ~1-line
    # pointer is appended within the <=30-line cap.
    summary, gate_verdict = _apply_pre_query_gate(
        summary,
        "get_report",
        project_id=project_id,
        metric_definitions=envelope.get("data", {}).get("metric_definitions"),
        envelope=envelope,
    )

    # P1 gate metrics (token-burn split evidence).
    latency_ms = int((time.perf_counter() - t0) * 1000)
    payload_bytes = len(json.dumps(envelope).encode("utf-8"))
    metrics_module.log_tool_metrics("get_report", summary, payload_bytes, latency_ms)

    # Story 13.5 volet (a) -- persister le snapshot de l'envelope gelee.
    # Best-effort : ne JAMAIS casser le rendu si la persistance echoue.
    # AD-9 : l'envelope est stockee telle quelle (fraicheur/provenance intactes).
    try:
        from core import db as _snap_db  # noqa: PLC0415
        from core.snapshots import persist_render_envelope as _persist_snap  # noqa: PLC0415

        with _snap_db.request_connection(identity) as _snap_conn:
            _persist_snap(
                project_id=project_id,
                tool_name="get_report",
                envelope=envelope,
                widget_uri=widget_uri,
                summary=summary,
                tool_args={"report_id": report_id, "date_from": date_from, "date_to": date_to},
                identity=identity,
                trace_id=trace_id,
                conn=_snap_conn,
            )
    except Exception as _snap_exc:  # noqa: BLE001
        logger.debug("get_report: snapshot_persist_skipped: %s", _snap_exc)

    # Story 50.6 -- no widget binding on a data tool, and no dataset in the model
    # channel. `widget_uri` is still resolved above because the snapshot persists it
    # (that is the Render's own pin, not a model-visible advertisement); what is
    # removed is the `_meta.ui` that ADVERTISED it to the host from a data tool.
    # Rationale and the enforcing guard: see get_daily_report. The split and the
    # refusal are the catalog-wide `on_call_tool` hook's, not this site's.
    meta = {}
    if gate_verdict is not None:
        meta["gate"] = gate_verdict

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
        meta=meta if meta else None,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind `get_report` on the given FastMCP app (declared -- AD-43)."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_report,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
