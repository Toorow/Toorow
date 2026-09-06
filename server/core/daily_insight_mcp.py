"""The Daily Insight surface: readiness, capabilities, preview and publish.

Four tools and the five helpers only they use -- the scope resolution, the input
resolution, the publishable-template set, the evidence refs and the snapshot
render. They moved here whole from `core.main`; nothing changed but the address
and the import of the entrypoint's own helpers.

`_project_topic_catalog` stays `core.cards_mcp`'s and is read from `core.main`,
which re-exports it: one catalogue, two readers, one owner.

The `from core.main import ...` lines inside the bodies are the seam every
extracted surface uses -- `core.main` imports this module, so a module-level
import back would be a cycle, and `_resolve_project` and `_loaded_modules` are
patched by the suites at the `core.main` address.
"""

from __future__ import annotations

import json
import logging

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import tracing
from core.mcp_scope import refuse_unless_project_scope

logger = logging.getLogger(__name__)




def _daily_insight_scope(
    project_id: str, *, minimum_capability: str = "view"
) -> tuple[str, str, bool]:
    """Resolve identity + project and enforce AD-5, mirroring get_card.

    Returns ``(identity, project_id, access_ok)``. ``access_ok`` is True whenever
    this function RETURNS: the gate refuses by raising, so there is no longer a
    path on which a caller reads with an unresolved access.

    ``strict`` is gone and ``minimum_capability`` takes its place (AI-269), because
    the two things it conflated are not the same question:

      - what happens when access cannot be RESOLVED. There is one right answer and
        it is now unconditional: `refuse_unless_project_scope` closes under
        failure. "The database was unreachable, so let the read through" is an
        open door with a cheap key.
      - what RANK the caller must hold. That difference is real and stays: the
        publish path demands ``edit``, because a write demanded at ``view`` lets a
        read-only holder publish a durable artifact in the neighbour's project.
    """

    from core.main import (  # noqa: PLC0415
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"
    project_id = _resolve_project(project_id, identity)
    # AI-269. This block raised `{"code": "forbidden"}` -- which tells a caller the
    # project EXISTS, where `project_not_found` would not -- and, on the read path,
    # swallowed a database failure as a pass. `refuse_unless_project_scope` is the
    # seam story 53.1 built: one refusal indistinguishable from an absent project,
    # and closed under failure for every caller.
    refuse_unless_project_scope(project_id, identity, minimum_capability=minimum_capability)
    return identity, project_id, True


def _resolve_daily_insight_inputs(
    project_id: str, date_from: str, date_to: str, *, identity: str
) -> tuple[set[str], set[str], str | None, list[str], list[dict]]:
    """Project's warehouse-derived availability + data-date freshness + DQ blockers + rows.

    Best-effort: returns empty availability / None freshness on a warehouse hiccup so the
    fail-closed validator refuses rather than the tool crashing (same posture as get_card).

    THE ROWS THEMSELVES ARE RETURNED NOW, and they were already read. Publication
    derives the insight's confidence from the rows carrying the members it CITED
    (`core.insight_confidence`), and re-querying the warehouse for that would let
    the availability the gate resolves refs against and the evidence the reading
    is measured over describe two different reads. One read, both answers.
    """
    from core import cards as _cards  # noqa: PLC0415
    from core import warehouse as _wh  # noqa: PLC0415

    available_metrics: set[str] = set()
    available_dimensions: set[str] = set()
    freshness_date: str | None = None
    rows: list[dict] = []
    try:
        rows = _wh.query_daily_report(
            project_id, date_from, date_to, None, include_prior_period=False
        )
        available_metrics, available_dimensions = _cards._available_inputs(rows)
        dates = [str(r.get("date"))[:10] for r in rows if r.get("date")]
        freshness_date = max(dates) if dates else None
    except Exception as _exc:  # noqa: BLE001
        rows = []
        logger.debug("daily_insight: availability_resolve_skipped: %s", _exc)

    dq_blocking: list[str] = []
    try:
        from core import db as _dqdb  # noqa: PLC0415
        from core.dq_api import fetch_dq_report_data  # noqa: PLC0415

        with _dqdb.request_connection(identity) as _conn_dq:
            report = fetch_dq_report_data(project_id, _conn_dq)
        for issue in (report or {}).get("issues", [])[:5]:
            msg = issue.get("message") or issue.get("type")
            if msg:
                dq_blocking.append(str(msg))
    except Exception as _exc:  # noqa: BLE001
        logger.debug("daily_insight: dq_resolve_skipped: %s", _exc)

    return available_metrics, available_dimensions, freshness_date, dq_blocking, rows


def _publishable_card_templates(project_id: str) -> tuple[set[str], str | None]:
    """The templates this project may publish against -- or NOTHING, and the reason.

    `_project_topic_catalog` returns `(catalog, reason)` and serves the PLATFORM
    DEFAULT SET whenever the reason is not None: no project named, project
    unresolvable, or the catalog store unreadable. That fallback is right for a
    DISCOVERY surface -- "these are the standard questions" is a true answer to
    "what can I ask?" -- and wrong for a PUBLICATION gate, where the same list
    silently re-admits a question this project retired, on the strength of a
    Postgres outage.

    Both publication call sites read only `[0]` and threw the reason away, so the
    availability of metrics and dimensions collapsed to empty on an outage
    (fail-closed) while the templates stayed full (fail-open) -- two thirds of
    one gate closing and one third opening, under a comment that swore all three
    closed. `list_card_templates` and `get_card_capabilities` already read the
    reason; this is the same pattern, with the opposite conclusion because
    publishing is not discovering.
    """

    from core.main import _project_topic_catalog  # noqa: PLC0415

    catalog, reason = _project_topic_catalog(project_id)
    if reason is not None:
        return set(), reason
    return {entry["id"] for entry in catalog}, None


def _resolve_evidence_refs(
    *,
    available_metrics: set[str],
    available_dimensions: set[str],
) -> set[str]:
    """Build the set of evidence refs that RESOLVE in server-produced data.

    Story 53.4 / CAV-07. This function used to `return set()` unconditionally,
    with a docstring that said so and a "Future:" comment above it. That was an
    honest placeholder and a real hole: because nothing could ever resolve,
    `evidenceRefs` had to stay OPTIONAL, so a published insight was allowed to
    cite nothing at all. What a reader saw next to a real, figure-bearing card
    was `summary`, `whyItMatters` and `recommendedAction` -- a thousand
    characters each of model-authored prose that no gate inspects -- and an
    unbacked causal claim borrowed the card's credibility.

    THE UNIVERSE IS WHAT THE SERVER ALREADY MEASURED, and it costs no query.
    Both inputs are resolved on the publication path anyway:
    `_resolve_daily_insight_inputs` returns the metrics and dimensions actually
    present in this project's warehouse rows for this window. Nothing here comes
    from the agent, which is the whole point -- the original docstring's promise
    ("never echoes the agent's own refs back as resolved") is kept by
    construction rather than by returning nothing.

    A THIRD KIND WAS HERE AND IT MADE THE REQUIREMENT A TAUTOLOGY.
    `card:<template>` was fed from `_project_topic_catalog`, i.e. from the very
    set gate 4 already checks `card.template` against -- so an insight that cited
    nothing but the card it renders passed the evidence gate having pointed at no
    datum. Worse, that catalog helper serves the platform defaults on an outage,
    so the one kind that could not collapse was the one that proved nothing. Both
    halves are closed: the kind is gone (`daily_insights_schema.EVIDENCE_KINDS`)
    and the template list itself now fails closed
    (`_publishable_card_templates`).

    A ref is `kind:value` over two kinds, declared in `daily_insights_schema`:

        metric:<name>     the server measured this metric in this window
        dimension:<name>  ... this dimension

    A ref of any other shape resolves against nothing and is refused as
    `evidence_malformed` -- unknown shapes are never ignored.

    EMPTY IS STILL POSSIBLE, AND NOW WHOLLY FAIL-CLOSED. A warehouse hiccup makes
    `_resolve_daily_insight_inputs` return empty availability by design, so this
    returns an empty universe and every publication is refused. That is the same
    posture the validator already takes, and it is the safe direction: an outage
    stops publication rather than letting an unbacked claim through.
    """
    from core.daily_insights_schema import evidence_universe  # noqa: PLC0415

    return evidence_universe(
        available_metrics=available_metrics,
        available_dimensions=available_dimensions,
    )


def _render_daily_insight_snapshot(
    project_id: str, identity: str, trace_id: str | None, payload: dict
) -> tuple[str | None, dict | None]:
    """Render a validated insight's card and freeze it; return ``(snapshot_id, envelope)``.

    Best-effort: reuses the EXISTING card render (`core.cards.get_card`) + snapshot store
    (`core.snapshots.persist_render_envelope`) so the published insight gains a
    `render_snapshot_id` lineage and becomes viewable ("View card"). The snapshot is NOT
    what makes it shareable -- a Share opens an `app.renders` row over a real Result, and
    the Result is produced by the publication's `result_fn` (`core.daily_insight_result`,
    AI-294). A render failure (e.g. empty warehouse) returns ``(None, None)`` -- the
    insight is still published, just without the frozen render until data exists.

    THE WHOLE CARD IS RETURNED, NOT ONLY ITS ID, and that is story 35's L3. The snapshot
    table is purged after `RENDER_SNAPSHOT_RETENTION_DAYS` (30) and the lineage column is
    `ON DELETE SET NULL`, so an id alone buys the insight thirty days of evidence and then
    leaves the model-authored prose standing on its own. Epic 35 §7 called for the card to
    live in the payload precisely because the two objects have different lifetimes.

    THE CARD IS NOT THE CHART. What toorow serves is a card an LLM can read, quote and
    add to, and that a person can share -- so all three parts `get_card` produces travel
    together and §6 names each of them: the resolved `envelope` (the figures, their
    provenance and the rendered comment), the `widgetUri` (which widget draws it -- an
    envelope with no declared renderer is data, not a card), and the `summary` (the
    model-channel reading, the part an LLM transmits). Keeping only the first would
    preserve the graph and lose the card.
    """
    from core.main import _loaded_modules  # noqa: PLC0415

    try:
        from core import cards as _cards  # noqa: PLC0415
        from core import db as _snap_db  # noqa: PLC0415
        from core.snapshots import persist_render_envelope as _persist  # noqa: PLC0415

        card = (payload or {}).get("card", {}) or {}
        period = (payload or {}).get("period", {}) or {}
        summary, envelope, widget_uri = _cards.get_card(
            _loaded_modules,
            project_id,
            template=card.get("template"),
            metrics=card.get("metrics"),
            report_ref=card.get("reportRef"),
            date_from=period.get("dateFrom", ""),
            date_to=period.get("dateTo", ""),
            plan_id=card.get("planId"),
            identity=identity,
            trace_id=trace_id,
        )
        with _snap_db.request_connection(identity) as _snap_conn:
            snapshot_id = _persist(
                project_id=project_id,
                tool_name="get_card",
                envelope=envelope,
                widget_uri=widget_uri,
                summary=summary,
                tool_args={
                    "template": card.get("template"),
                    "metrics": card.get("metrics"),
                    "report_ref": card.get("reportRef"),
                    "date_from": period.get("dateFrom", ""),
                    "date_to": period.get("dateTo", ""),
                    "plan_id": card.get("planId"),
                },
                identity=identity,
                trace_id=trace_id,
                conn=_snap_conn,
            )
        # The card travels even when the snapshot could not be written: the id is the
        # lineage, the card is the evidence, and losing one is not losing both.
        return snapshot_id, {
            "envelope": envelope,
            "widgetUri": widget_uri,
            "summary": summary,
        }
    except Exception as _exc:  # noqa: BLE001 - best-effort; publish proceeds without lineage
        logger.debug("daily_insight: render_snapshot_skipped: %s", _exc)
        return None, None


def get_daily_insight_readiness(project_id: str, insight_date: str) -> ToolResult:
    """Is J-1 data ready to research for ``insight_date``? -- ready | blocked (+ reasons).

    AD-5 scoped. ``blocked`` is a DISTINCT state from "no insight": it means the task must
    STOP (data not ready), never publish stale data as current.
    """
    from datetime import date as _date  # noqa: PLC0415
    from datetime import timedelta as _timedelta  # noqa: PLC0415

    from core import daily_insights_tools as _dit  # noqa: PLC0415

    identity, project_id, _access = _daily_insight_scope(project_id)
    # Look back a week ending at the target to establish the latest available data date.
    try:
        week_start = (_date.fromisoformat(insight_date) - _timedelta(days=7)).isoformat()
    except (ValueError, TypeError):
        week_start = insight_date
    _m, _d, freshness_date, dq_blocking, _rows = _resolve_daily_insight_inputs(
        project_id, week_start, insight_date, identity=identity
    )
    result = _dit.readiness(
        insight_date=insight_date, freshness_date=freshness_date, dq_blocking=dq_blocking
    )
    summary = f"Readiness for {insight_date}: {result['status'].upper()}."
    if result["reasons"]:
        summary += " " + " ".join(result["reasons"][:3])
    return ToolResult(content=[TextContent(type="text", text=summary)], structured_content=result)


def get_card_capabilities(project_id: str, date_from: str = "", date_to: str = "") -> ToolResult:
    """What can this project render right now? -- satisfiable cards + available fields (35.1).

    AD-5 scoped. Zero duplicated vocabulary: the catalogue is derived from the card registry.

    Story 52.1 AC5, WIRED HERE. `daily_insights_card_contract` is pure by design: it
    holds no connection, so the caller that has one resolves the Project's catalog
    and passes it down. That is this function -- and for one epic it did not, so
    this tool went on advertising the nine platform questions while
    `preview_daily_insight`, two hundred lines below, refused the very template it
    had just advertised. A parameter added and never passed is a rewiring in
    signature only.
    """
    from core import cards as _cards  # noqa: PLC0415
    from core import daily_insights_tools as _dit  # noqa: PLC0415
    from core.main import _project_topic_catalog  # noqa: PLC0415

    identity, project_id, _access = _daily_insight_scope(project_id)
    start, end = _cards._resolve_window(date_from, date_to)
    available_metrics, available_dimensions, _fresh, _dq, _rows = _resolve_daily_insight_inputs(
        project_id, start, end, identity=identity
    )
    catalog, catalog_reason = _project_topic_catalog(project_id)
    caps = _dit.capabilities(
        available_metrics=available_metrics,
        available_dimensions=available_dimensions,
        catalog=catalog,
        catalog_reason=catalog_reason,
    )
    n_ok = sum(1 for e in caps["catalog"] if e["satisfiable"])
    summary = f"{n_ok} renderable card(s) for this project; contract v{caps['contractVersion']}."
    if catalog_reason is not None:
        summary += (
            " Catalog is the platform default set, not this project's "
            f"({catalog_reason})."
        )
    # Story 50.6 -- this is a catalog, not warehouse rows, and it was still
    # unbounded IN CODE: `daily_insights_tools.capabilities` returned every metric,
    # every dimension and the whole catalog, so a project with a wide semantic
    # surface put an unbounded list in front of the model. The lists are bounded at
    # the source now and state what was withheld; the catalog-wide `on_call_tool`
    # hook routes any remainder to the app channel rather than dropping it.
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=caps,
    )


def preview_daily_insight(project_id: str, payload: dict) -> ToolResult:
    """Validate a candidate insight WITHOUT publishing (35.0 fail-closed validator).

    AD-5 scoped. Never persists, never writes a snapshot. Returns {ok, reasonCode?} so the
    agent can fix the spec before calling publish_daily_insights.
    """
    from core import cards as _cards  # noqa: PLC0415
    from core import daily_insights as _store  # noqa: PLC0415
    from core import daily_insights_tools as _dit  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415

    identity, project_id, access_ok = _daily_insight_scope(project_id)
    period = (payload or {}).get("period", {})
    start, end = _cards._resolve_window(period.get("dateFrom", ""), period.get("dateTo", ""))
    available_metrics, available_dimensions, freshness_date, _dq, _rows = (
        _resolve_daily_insight_inputs(
            project_id, start, end, identity=identity
        )
    )
    # Story 52.1: the Project's own catalog, not the platform list -- a question
    # this Project retired is not a template it can publish against. Story 53.4:
    # and if the catalog could not be resolved, NOTHING is publishable -- the
    # reason travels so the refusal says which fact caused it.
    available_templates, catalog_reason = _publishable_card_templates(project_id)
    existing_slots: set[int] = set()
    _date_to = (payload or {}).get("period", {}).get("dateTo", "")
    try:
        with _core_db.request_connection(identity) as _conn:
            run = _store.get_run(project_id, _date_to, _conn)
        existing_slots = {int(i["slot"]) for i in (run or {}).get("insights", [])}
    except Exception as _exc:  # noqa: BLE001
        logger.debug("preview_daily_insight: existing_slots_skipped: %s", _exc)

    out = _dit.preview(
        payload=payload or {},
        available_metrics=available_metrics,
        available_dimensions=available_dimensions,
        available_templates=available_templates,
        catalog_reason=catalog_reason,
        # Evidence resolves in SERVER data, never self-certified by the agent's own refs
        # (story 53.4). The universe is what this project MEASURED in this window -- a card
        # template is the form, not the datum, and is no longer a citable kind.
        resolvable_evidence=_resolve_evidence_refs(
            available_metrics=available_metrics,
            available_dimensions=available_dimensions,
        ),
        freshness_date=freshness_date,
        has_project_access=access_ok,
        existing_slots=existing_slots,
    )
    verdict = "OK" if out["ok"] else f"REJECTED ({out['reasonCode']})"
    return ToolResult(
        content=[TextContent(type="text", text=f"Preview {verdict}.")], structured_content=out
    )


def publish_daily_insights(
    project_id: str,
    insight_date: str,
    items: list[dict] | None = None,
    status: str = "published",
) -> ToolResult:
    """Publish 0..3 insights atomically for ``insight_date`` (AD-5, idempotent per slot).

    Validates EVERY item fail-closed (all-or-nothing: one bad item publishes nothing), then
    persists the run + items in one transaction (35.3). ``status`` other than 'published'
    records a distinct run (no_insight / blocked / failed) with zero items. Returns a short
    write-ack {runId, status, publishedSlots}.
    """
    from datetime import date as _date  # noqa: PLC0415
    from datetime import timedelta as _timedelta  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import daily_insights as _store  # noqa: PLC0415
    from core import daily_insights_tools as _dit  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415
    from core import insight_confidence as _insight_confidence  # noqa: PLC0415

    # WRITE path: `edit`, never `view`. Failing closed on an unresolvable check is
    # no longer this call's business -- the seam does it for every caller (AI-269).
    # What this line still decides is the RANK: publishing a durable artifact in a
    # project is not something a read-only holder may do.
    identity, project_id, access_ok = _daily_insight_scope(project_id, minimum_capability="edit")
    trace_id = tracing.current_trace_id_hex() or None
    items = items or []

    # Freshness over a LOOKBACK window (not [insight_date, insight_date], which is
    # self-referential): max(row date) is the true latest available data date, so a pipeline
    # that is behind the claimed date is caught by the validator's stale_data gate.
    try:
        _win_start = (_date.fromisoformat(insight_date) - _timedelta(days=7)).isoformat()
    except (ValueError, TypeError):
        _win_start = insight_date
    available_metrics, available_dimensions, freshness_date, dq_blocking, evidence_rows = (
        _resolve_daily_insight_inputs(
            project_id, _win_start, insight_date, identity=identity
        )
    )
    # The SERVER's own reading of whether this date was ready. `dq_blocking` used to be
    # discarded here, which left `publish` with no way to tell a day that held nothing
    # from a day nobody could look at -- and it recorded whichever of the two the agent
    # declared. Same reading the readiness tool returns, resolved on the write path.
    readiness_state = _dit.readiness(
        insight_date=insight_date,
        freshness_date=freshness_date,
        dq_blocking=dq_blocking,
    )
    # Story 52.1: the Project's own catalog, not the platform list -- a question
    # this Project retired is not a template it can publish against. Story 53.4:
    # a catalog that could not be resolved publishes NOTHING (this is the WRITE
    # path -- it produces a durable artifact, so it fails closed like the scope
    # check three lines above).
    available_templates, catalog_reason = _publishable_card_templates(project_id)

    # Run-level analyzed window = min/max of the published items' own periods (not a single day).
    _froms = [it.get("period", {}).get("dateFrom") for it in items if it.get("period")]
    _tos = [it.get("period", {}).get("dateTo") for it in items if it.get("period")]
    _froms = [d for d in _froms if d]
    _tos = [d for d in _tos if d]
    period_from = min(_froms) if _froms else insight_date
    period_to = max(_tos) if _tos else insight_date

    try:
        with _core_db.request_connection(identity) as _conn:
            # AI-294 (design 2026-08-17): publishing an insight produces a Result.
            # The derivation, the spec version and the execution all ride THIS
            # connection, so the lineage commits with the run inside `record_run`
            # and a refused run discards everything. `org_id` is required by the
            # existing writers (`create_query_spec_version`, `accept_execution`);
            # a project without one publishes with the reason named instead.
            from core import daily_insight_result as _dir  # noqa: PLC0415

            _org_id = _dir.resolve_org_id(_conn, project_id)
            if _org_id is None:
                _result_fn = lambda p: {  # noqa: E731
                    "result_unavailable_reason": (
                        "This project resolves to no organization, so the governed "
                        "execution path cannot write for it."
                    ),
                    "missing_link": "org_id",
                }
            else:
                _result_fn = lambda p: _dir.produce_insight_result(  # noqa: E731
                    _conn,
                    org_id=_org_id,
                    project_id=project_id,
                    payload=p,
                    actor=identity,
                )
            existing = _store.get_run(project_id, insight_date, _conn)
            existing_slots = {int(i["slot"]) for i in (existing or {}).get("insights", [])}
            validate_ctx = {
                "available_metrics": available_metrics,
                "available_dimensions": available_dimensions,
                "available_templates": available_templates,
                "resolvable_evidence": _resolve_evidence_refs(
                    available_metrics=available_metrics,
                    available_dimensions=available_dimensions,
                ),
                "freshness_date": freshness_date,
                "has_project_access": access_ok,
                "existing_slots": existing_slots,
            }
            ack = _dit.publish(
                project_id=project_id,
                insight_date=insight_date,
                status=status,
                item_payloads=items,
                validate_ctx=validate_ctx,
                catalog_reason=catalog_reason,
                conn=_conn,
                identity=identity,
                trace_id=trace_id,
                readiness_state=readiness_state,
                period_from=period_from,
                period_to=period_to,
                # Render each VALIDATED item's card + freeze a snapshot so the insight is
                # viewable (render_snapshot_id lineage). Best-effort: a render
                # failure (e.g. empty warehouse) leaves the row publishable without lineage.
                render_fn=lambda p: _render_daily_insight_snapshot(
                    project_id, identity, trace_id, p
                ),
                # AI-294: derive a governed Query Spec behind the card and execute it,
                # so the publication names a Result -- the object the ONE Share
                # mechanism (Render -> Share) operates on. A derivation refusal never
                # blocks publication; it is stored named on the item.
                result_fn=_result_fn,
                # The confidence a reader sees is MEASURED here, from the rows
                # already read above, restricted to the members this insight
                # cites and to its own period. The level the model declared
                # about its own claim survives as `authorship.declaredConfidence`
                # and drives nothing (`proactive-assertions.md`: a confidence
                # level declared by the author of the claim is not derived).
                confidence_fn=lambda p: _insight_confidence.derive_insight_confidence(
                    p, rows=evidence_rows
                ),
            )
    except ToolError:
        raise
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "invalid_request", "message": str(exc)}))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(json.dumps({"code": "publish_failed", "message": str(exc)}))

    if not ack["ok"]:
        raise ToolError(
            json.dumps(
                {
                    "code": "validation_failed",
                    "reason_code": ack.get("reasonCode"),
                    "message": ack.get("message"),
                    "slot": ack.get("slot"),
                }
            )
        )
    summary = (
        f"Published {len(ack['publishedSlots'])} insight(s) for {insight_date} "
        f"(run {ack['runId']}, status {ack['status']})."
    )
    return ToolResult(content=[TextContent(type="text", text=summary)], structured_content=ack)


def retract_daily_insight(project_id: str, insight_id: str, reason: str) -> ToolResult:
    """Withdraw a published daily insight -- an audited state transition, never a delete.

    The row STAYS and is served as withdrawn: it is the evidence that the claim was
    made, and destroying it would lose the difference between "this was never said"
    and "this was said, and later withdrawn" (`proactive-assertions.md`, decision 4;
    migration 321).

    ``reason`` is REQUIRED and is read by people: without it, the next reader cannot
    tell a wrong insight from an inconvenient one. A retraction is never unmade -- if
    a different reading is wanted, publish it on a free slot of the same day.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import daily_insights as _store  # noqa: PLC0415
    from core import db as _core_db  # noqa: PLC0415

    # WRITE path: `edit`, exactly like `publish_daily_insights`. Unsaying an
    # assertion in a project is not something a read-only holder may do.
    identity, project_id, _access_ok = _daily_insight_scope(
        project_id, minimum_capability="edit"
    )

    try:
        with _core_db.request_connection(identity) as _conn:
            retracted = _store.retract_insight(
                project_id=project_id,
                insight_id=insight_id,
                retracted_by=identity,
                reason=reason,
                conn=_conn,
            )
    except _store.DailyInsightRefusal as refusal:
        raise ToolError(json.dumps({"code": refusal.code, "message": refusal.message}))
    except Exception as exc:  # noqa: BLE001
        raise ToolError(json.dumps({"code": "retract_failed", "message": str(exc)}))

    ack = {
        "insightId": str(retracted.get("id")),
        "insightDate": str(retracted.get("insight_date")),
        "slot": retracted.get("slot"),
        "retractedAt": str(retracted.get("retracted_at")),
        "retractedBy": str(retracted.get("retracted_by")),
        "retractedReason": str(retracted.get("retracted_reason")),
        # Said in the ack so the calling task learns the rule instead of retrying.
        "note": (
            "The insight is kept and shown as withdrawn, with this reason. A "
            "retraction is never unmade; publish a new reading on a free slot of "
            "the same day."
        ),
    }
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Retracted insight {ack['insightId']} of {ack['insightDate']}. "
                    f"It stays visible as withdrawn."
                ),
            )
        ],
        structured_content=ack,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the five Daily Insight tools on the given FastMCP app (declared -- AD-43).

    Three reads -- readiness, capabilities and the fail-closed validator, which
    explicitly "never persists, never writes a snapshot" -- and two writes.
    Publishing insights is the act that makes something the product ASSERTS to a
    person who did not ask (`proactive-assertions.md`), so it is declared under the
    profile that governs publication rather than reaching the default catalog by
    omission, as it did until 2026-08-12.

    RETRACTION CARRIES THE SAME DECLARATION AS PUBLICATION, and that is deliberate.
    It is the same object being written -- a durable, project-scoped claim the
    product volunteered -- and unsaying one is exactly as consequential as saying
    it: readers have already seen it. Declaring the withdrawal any lighter than the
    assertion would let a confirmation-free door undo a confirmed one.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_daily_insight_readiness,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_card_capabilities,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        preview_daily_insight,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    # TWO LITERAL CALLS, not a loop (review follow-up 2026-08-30): the
    # legacy-analytics census reads the SECOND ARGUMENT's name from the AST, so
    # a loop variable turns both tools into the anonymous `#tool:handler` and
    # the inventory line naming `publish_daily_insights` stops matching. A
    # registration that hides its tool's name from the instrument that audits
    # producers is not worth the four lines it saves.
    register_profiled(
        mcp,
        publish_daily_insights,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        retract_daily_insight,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
