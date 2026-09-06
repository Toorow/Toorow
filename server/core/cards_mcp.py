"""The card library MCP surface: `list_card_templates` and `get_card`.

These are the LLM-agent channel of the card selection contract; `core.cards_api`
mirrors them over REST calling the SAME `core.cards` functions, so the two
surfaces cannot drift. The bodies resolve identity and project, enrich the meta
with context events and alerts, and return the dual-channel ToolResult (AD-1).
All selection and render logic lives in `core.cards`.

`_project_topic_catalog` lives here because `list_card_templates` is its first
reader; `core.daily_insight_mcp` reads it back through `core.main`, which
re-exports it -- one catalogue, one owner.

The `from core.main import ...` lines inside the bodies are the seam every
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
from core.mcp_scope import refuse_unless_project_scope

logger = logging.getLogger(__name__)




_MAX_TEMPLATE_LINES = 28

# Story 50.6 -- what a model needs in order to CHOOSE a card, and nothing else.
#
# This tool shipped a 7255-byte `structuredContent` against a 4096-byte budget, in
# the assembled catalog, guarded by nothing. Two of the eleven serialized keys were
# carrying the weight and neither belongs in the model channel:
#
#   * `composition` (3938 of those bytes) is the RENDERER's layout -- block types
#     and their bindings. A model choosing between cards never reads it; the
#     renderer does, and it reads it from the card registry, not from here.
#   * `widget_uri` is an app resource URI, which is the same wrong-channel defect
#     `app_resource` was on the three capability tools (AC4): a model-visible field
#     advertising an app resource, on a data tool.
#
# So this is a projection, not a truncation: every registered template still
# appears, with every field that bears on the choice, and the count is stated.
_TEMPLATE_CHOICE_FIELDS = (
    "id",
    "title",
    "answers_question",
    "kind",
    "fallback_rank",
    "required_metrics",
    "required_dimensions",
    "optional_metrics",
    "optional_dimensions",
)


def _project_topic_catalog(project_id: str) -> tuple[list[dict], str | None]:
    """Resolve one Project's Answerable Topic catalog for a tool body (Story 52.1).

    Returns ``(catalog, reason_code)``. `reason_code` is None only when the list IS
    this Project's resolved catalog; otherwise the list is the platform default set
    served as a FALLBACK and the code says which fact produced it.

    THE ORDER MATTERS, and it is the reason this is a helper rather than three
    inline reads. Authorization comes FIRST: an identity that cannot read the
    Project never reaches its catalog, and the refusal yields the platform default
    set rather than an error, because the catalog is a discovery surface and "these
    are the standard questions" is a true answer, while "these are that other
    Project's questions" would not be.

    THE REFUSAL IS NO LONGER SILENT, and that is the repair. Three different facts
    -- nothing is configured, the store is unreadable, this identity may not read
    the Project -- left this function through one door, with one payload and no
    marker. A Project that had retired six of the nine questions was told, in the
    same shape as a Project that had configured nothing, that it answers all nine.
    Story 52.2 built three reason codes to stop exactly that on the query store;
    the catalog is the root object and had none. Serving the defaults stays
    defensible; serving them MUTE does not.

    A scope check that CANNOT run refuses too. `get_card` fails open here (it
    swallows the exception and continues); that shape is a recorded caveat and is
    deliberately not copied.
    """
    from core import answerable_topics as _topics  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _resolve_project,
        get_access_token,
    )

    raw = (project_id or "").strip()
    if not raw:
        return _topics.default_catalog(), _topics.CATALOG_NO_PROJECT_REASON

    from core import db as _core_db  # noqa: PLC0415
    from core.project_access import identity_can_read_project  # noqa: PLC0415

    # THE IDENTITY IS READ BEFORE THE PROJECT, not after (2026-08-25). Since the
    # arbitration of `mcp-tool-surface.md` the resolver decides existence FOR a
    # named caller; reading the token below it would have resolved the project
    # anonymously and then asked about someone else.
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    try:
        resolved = _resolve_project(raw, identity)
    except Exception as exc:  # noqa: BLE001 -- unknown/archived/INVISIBLE: defaults, no leak
        # SWALLOWED ON PURPOSE, and it is one of the only two places allowed to.
        # What is served here is the DEFAULT catalogue -- product vocabulary that
        # ships with the product -- never a line of the named project. A caller
        # that swallowed this refusal and still served the project's content
        # would be the `get_procedure` defect of 2026-08-17 again.
        logger.debug("list_card_templates: project_resolution_skipped: %s", exc)
        return _topics.default_catalog(), _topics.CATALOG_ACCESS_DENIED_REASON

    try:
        with _core_db.request_connection(identity) as conn:
            if not identity_can_read_project(resolved, identity, conn):
                return _topics.default_catalog(), _topics.CATALOG_ACCESS_DENIED_REASON
            return _topics.resolve_catalog_with_reason(resolved, conn)
    except Exception as exc:  # noqa: BLE001 -- unreadable store: the defaults, never a guess
        logger.debug("list_card_templates: catalog_read_skipped: %s", exc)
        return _topics.default_catalog(), _topics.CATALOG_UNAVAILABLE_REASON


def list_card_templates(project_id: str = "") -> ToolResult:
    """Discoverable card catalog -- the LLM reads this to CHOOSE a template.

    Returns the AD-1 envelope whose ``data.templates`` lists every registered card
    (id, the FR question it answers, required canonical inputs, fallback_rank). The
    <=30-line summary enumerates them so Claude can pick the best card for the user's
    question and then call ``get_card(template=<id>)``.

    ``data.templates`` carries the choice-relevant projection of each template
    (``_TEMPLATE_CHOICE_FIELDS``); the renderer layout and the widget URI are not a
    model's business and are not sent. ``templates_total`` states how many templates
    exist and ``templates_withheld`` how many the bound left out, so a shortened
    catalog can never read as the whole one.

    Story 52.1: the catalog is the PROJECT's, not the platform's. ``project_id``
    resolves through the same helper ``get_card`` uses; a Project that configured
    nothing sees exactly the nine default questions, and one that reworded, added or
    retired a question sees its own. AD-5 is enforced before its catalog is read:
    an identity that cannot read the Project gets the platform default set, never
    another Project's questions.

    ``catalog_status`` is ``resolved`` when the list is this Project's own, and
    ``defaults_only`` when it is the platform fallback -- with ``catalog_reason``
    naming which fact caused the fallback. A degraded catalog that does not say so
    is indistinguishable from an exact one, and a model would then advertise a
    question the Project deliberately stopped answering.

    Parameters:
        project_id: Project identifier. Omitted => the platform default set.
    """
    from core import cards as _cards  # noqa: PLC0415
    from core.main import _envelope  # noqa: PLC0415
    from core.model_channel import bounded_head  # noqa: PLC0415

    templates, catalog_reason = _project_topic_catalog(project_id)
    head, withheld = bounded_head(templates, _MAX_TEMPLATE_LINES)
    envelope = _envelope(
        {
            "templates": [
                {k: tpl.get(k) for k in _TEMPLATE_CHOICE_FIELDS if k in tpl} for tpl in head
            ],
            "templates_total": len(templates),
            "templates_withheld": withheld,
            "catalog_status": "resolved" if catalog_reason is None else "defaults_only",
            "catalog_reason": catalog_reason,
        }
    )

    lines = ["Cartes disponibles (choisissez selon la question) :"]
    if catalog_reason is not None:
        # The model channel must carry the caveat, not only the structured field:
        # a summary that reads like the project's catalog IS the claim being made.
        lines.append(
            f"[platform default set -- not this project's catalog ({catalog_reason})]"
        )
    for tpl in templates[: _MAX_TEMPLATE_LINES]:
        # Story 9.8: a context card (kind=context) answers an inventory question from
        # app-level sources (no fact metrics). Label it as such -- it is explicit-only
        # (never auto-suggested), so the LLM must request it by id.
        if tpl.get("kind") == _cards.CARD_KIND_CONTEXT:
            lines.append(
                f"- {tpl['id']} : « {tpl['answers_question']} » "
                "[carte contexte, sur demande explicite]"
            )
            continue
        # Humanise the ANY_METRIC "*" sentinel so the LLM reads "any metric" rather than a
        # glob token (review-9-1 F-6). Same phrasing as the empty-list fallback.
        req_names = [
            "(any metric)" if m == _cards.ANY_METRIC else m
            for m in tpl["required_metrics"]
        ]
        reqs = ", ".join(req_names or ["(any metric)"])
        lines.append(f"- {tpl['id']} : « {tpl['answers_question']} » [requiert : {reqs}]")
    summary = "\n".join(lines[:30])

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
    )




def get_card(
    project_id: str,
    template: str | None = None,
    metrics: list[str] | None = None,
    report_ref: str | None = None,
    date_from: str = "",
    date_to: str = "",
    plan_id: str | None = None,
) -> ToolResult:
    """Resolve a question-answering card -- lean summary + full envelope + widget.

    # AD-14: identity resolved from OAuth 2.1 + PKCE. AD-5: project scoping enforced.

    CONSULT CONTEXT FIRST (AD-18): before interpreting this card, consult the
    governed context layer -- ``search_context`` (a metric/concept) or
    ``get_procedure`` (a named playbook). Not a gate: the card still renders if you
    skip it, but adherence is measured per session (Epic 14).

    Row resolution is source-agnostic (AD-2): pass ``report_ref`` ("{connector}/{report}")
    to render a named report as a card, or ``metrics=[...]`` (canonical names) for an
    ad-hoc cross-connector card. When ``template`` is omitted the server SUGGESTS the
    best card whose required inputs are satisfied (highest fallback_rank) and reports
    the choice + alternatives in ``meta.card_selection`` so you can re-request a
    different one.

    THIS TOOL MOUNTS NO WIDGET, and the sentence that said it bound one through
    ``_meta.ui.resourceUri`` is removed rather than left to be believed. Story 50.6
    took the binding away from every data tool, and
    ``mcp_profiles._record_app_declaration`` now aborts registration for any tool
    but ``render_analyze_result`` that declares a widget resource -- so the claim
    could not have been true and cannot become true here. The chosen template still
    travels as ``meta.answer.visual.widget_uri``, which NAMES the bundle; it does
    not mount it.

    The envelope carries the deterministic cited comment (``data.rendered_comment``,
    AD-9) AND -- when a report override exists -- R6 ``metric_definitions`` +
    ``llm_commentary_guidelines`` so you can deepen the analysis per the user's question.

    Parameters:
        project_id: Project identifier.
        template:   Optional card id ("kpi"). Omitted => server suggestion.
        metrics:    Optional canonical metric names (ad-hoc mode).
        report_ref: Optional "<connector>/<report_id>" (report-backed mode). The
                    connector part is the connector key -- on the wire the field
                    is still spelled `module_name`, and renaming wire keys is a
                    recorded migration, not this docstring's business
                    (vocabulary: wire-field).
        date_from:  ISO-8601 start; empty => today - 30d.
        date_to:    ISO-8601 end; empty => today.
        plan_id:    Media plan id -- required by the explicit ``mediaplan_pacing``
                    context card (Story 22.5), ignored by every other template.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import cards as _cards  # noqa: PLC0415
    from core.context_events import (  # noqa: PLC0415
        ContextEventsUnavailable as _ContextEventsUnavailable,
    )
    from core.main import (  # noqa: PLC0415
        _fetch_context_events,  # noqa: PLC0415
        _loaded_modules,
        _resolve_project,
        get_access_token,
    )

    # The gate is imported from the module that DEFINES it, not from the
    # `core.main` re-export: the append census of `mcp_tool_surface_report.py`
    # resolves a name only to a function of the module it is imported from, so
    # through `core.main` the `app.query_adherence` write of `get_card` was
    # invisible to it (2026-09-01).
    from core.reporting_mcp import _apply_pre_query_gate  # noqa: PLC0415
    t0 = time.perf_counter()
    token: AccessToken | None = get_access_token()
    identity = token.claims.get("sub", token.client_id) if token else "anonymous"

    project_id = _resolve_project(project_id, identity)

    # AD-5: enforce project scope before any warehouse read.
    #
    # AI-269. This block used to raise `{"code": "forbidden"}` and then swallow any
    # exception as a PASS. Two defects in eleven lines:
    #
    #   - `forbidden` says "this project exists and you may not have it", while a
    #     project that does not exist raises `project_not_found`. Comparing the two
    #     refusals is how a caller enumerates the neighbours' projects.
    #   - the `except Exception` fell through to the read. The cheapest route to
    #     someone else's data was to make the database unreachable.
    #
    # `mcp_scope.refuse_unless_project_scope` is the seam story 53.1 built for its
    # own 27 tools, and it holds both properties: one indistinguishable refusal,
    # and a failure that looks like an absent project rather than an open door.
    refuse_unless_project_scope(project_id, identity)

    trace_id = tracing.current_trace_id_hex() or None

    # Context events + alerts for the resolved window. AI-344 (2026-09-01): the
    # events read is no longer "[] on DB down" -- it serves from the mirror or the
    # record, and when neither can serve the card is still built and its `meta`
    # says the events were not read, rather than showing a quiet window.
    start, end = _cards._resolve_window(date_from, date_to)
    context_events_unavailable: dict | None = None
    try:
        context_events = _fetch_context_events(project_id, start, end, identity=identity)
    except _ContextEventsUnavailable as exc:
        context_events = []
        context_events_unavailable = exc.payload
    _alerts: list[dict] = []
    try:
        from core import business_alerts as _ba  # noqa: PLC0415
        from core.db import request_connection as _armed  # noqa: PLC0415

        # 67-1 (2026-08-24). This site was NEVER in the debt count: the AST
        # sweep of `test_mcp_surfaces_acquire_an_armed_connection.py` matched
        # the called name, and `get_connection as _pg` renamed itself out of
        # the measurement. `refuse_unless_project_scope` runs eleven lines
        # above, so the identity was there the whole time -- the floor simply
        # was not armed underneath the alert read.
        with _armed(identity) as _conn_a:
            _alerts = _ba.fetch_recent_alert_firings(project_id, _conn_a, hours=24)
            _alerts = _alerts + _ba.fetch_recent_meta_alerts(project_id, _conn_a, hours=24)
    except Exception as _exc:  # noqa: BLE001
        logger.debug("get_card: alerts_fetch_skipped: %s", _exc)

    try:
        summary, envelope, widget_uri = _cards.get_card(
            _loaded_modules,
            project_id,
            template=template,
            metrics=metrics,
            report_ref=report_ref,
            date_from=date_from,
            date_to=date_to,
            plan_id=plan_id,
            identity=identity,
            context_events=context_events,
            alerts=_alerts,
            trace_id=trace_id,
            context_events_unavailable=context_events_unavailable,
        )
    except _cards.CardTemplateNotFound as exc:
        raise ToolError(
            json.dumps({"code": "not_found", "message": f"Template inconnu : {exc.args[0]}"})
        )
    except _cards.CardTemplateUnsatisfied as exc:
        raise ToolError(
            json.dumps(
                {
                    "code": "unsatisfiable_template",
                    "message": f"Template '{exc.template_id}' requires data that is absent.",
                    "missing": exc.missing,
                }
            )
        )
    except Exception as exc:
        raise ToolError(
            json.dumps(
                {"code": "warehouse_query_error", "message": f"Card render failed: {exc}"}
            )
        )

    envelope["data"]["identity"] = identity
    if context_events_unavailable is not None:
        # Beside `meta.context_events`, never instead of it: the card's window is
        # not quiet, it is unread, and the reader must be able to tell the two apart.
        envelope.setdefault("meta", {})["context_events_unavailable"] = (
            context_events_unavailable
        )

    # Story 11.6 (AD-18): measure the pre-query gate + optional search_context
    # pointer (never enforce). get_card carries R6 metric_definitions on the
    # envelope (AI-50) when a report override defines them.
    summary, gate_verdict = _apply_pre_query_gate(
        summary,
        "get_card",
        project_id=project_id,
        metric_definitions=envelope.get("data", {}).get("metric_definitions"),
        envelope=envelope,
    )

    latency_ms = int((time.perf_counter() - t0) * 1000)
    payload_bytes = len(json.dumps(envelope).encode("utf-8"))
    metrics_module.log_tool_metrics("get_card", summary, payload_bytes, latency_ms)

    # Story 13.5 volet (a) -- persister le snapshot de l'envelope gelee.
    # Best-effort : ne JAMAIS casser le rendu si la persistance echoue.
    # AD-9 : l'envelope est stockee telle quelle (fraicheur/provenance intactes).
    try:
        from core import db as _snap_db_c  # noqa: PLC0415
        from core.snapshots import persist_render_envelope as _persist_snap_c  # noqa: PLC0415

        with _snap_db_c.request_connection(identity) as _snap_conn_c:
            _persist_snap_c(
                project_id=project_id,
                tool_name="get_card",
                envelope=envelope,
                widget_uri=widget_uri,
                summary=summary,
                tool_args={
                    "template": template,
                    "metrics": metrics,
                    "report_ref": report_ref,
                    "date_from": date_from,
                    "date_to": date_to,
                    "plan_id": plan_id,
                },
                identity=identity,
                trace_id=trace_id,
                conn=_snap_conn_c,
            )
    except Exception as _snap_exc_c:  # noqa: BLE001
        logger.debug("get_card: snapshot_persist_skipped: %s", _snap_exc_c)

    # Story 50.6 -- no widget binding on a data tool, and no dataset in the model
    # channel (rationale and enforcing guard: see get_daily_report). The chosen
    # template still rides the persisted snapshot; it is no longer advertised to
    # the host from a data tool result. The split and the refusal are the
    # catalog-wide `on_call_tool` hook's, not this site's.
    # Story 52.4: the answer contract rides `meta`, not `structuredContent`.
    # `enforce_model_channel` measures `structured_content` alone, and the `dedup`
    # envelope sits a few dozen bytes under the 4096-byte budget (117 was written
    # here once and never reproduced: 127, then 65 -- a reading of the tree, not a
    # constant; `tests/integration/test_model_channel_margin.py` measures it and
    # fails before the cliff). A contract inside it
    # would have displaced `composition` into the app channel, which is the
    # contract breaking the answer it exists to describe. `meta` is where a host
    # binding belongs (the widget resource URI already travels there), and both
    # hosts build the object with the SAME `answer_contract.build_answer`.
    from core.answer_contract import build_answer as _build_answer  # noqa: PLC0415

    # NO OBSERVATION HANDLE IS MINTED HERE, and the reason is a measurement, not
    # an omission (clause 19 of `docs/product-architecture/mcp-tool-surface.md`,
    # amendment of 2026-08-30).
    #
    # The clause names this tool as "the producer that mounts them" for the card
    # feedback bars. It does not mount them, and it cannot:
    #
    #   * `mcp_profiles.assert_data_render_split`, run against the LIVE registry,
    #     returns exactly one binding -- `render_analyze_result` ->
    #     the visualization-runtime resource URI. `_record_app_declaration` raises
    #     `CatalogValidationError` for any other tool that declares a widget
    #     resource, so a `_meta.ui` binding added here would abort registration.
    #   * a grant is a row of `app.result_app_grants` (migration 161) whose
    #     `(result_id, org_id, project_id)` is a FOREIGN KEY into
    #     `app.query_results`. A card is not a Query Spec Result -- the only writer
    #     of that table is `core.query_execution` -- so `issue_handle` has nothing
    #     to be issued over, and `core.app_observation_handle` names the gesture
    #     "call analyze_result or render_analyze_result", which is the analyze
    #     path's gesture and not a card's.
    #
    # So the append the card bars exist for stays impossible until a decision is
    # taken on WHAT a card observation is issued over. Fabricating a handle here,
    # or binding one to a Result this card never read, would make the refusal
    # disappear without making the observation true.
    tool_meta: dict = {"answer": _build_answer(envelope, widget_uri=widget_uri)}
    if gate_verdict is not None:
        tool_meta["gate"] = gate_verdict

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=envelope,
        meta=tool_meta,
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the two card-library tools on the given FastMCP app.

    Declared (AD-43): both are reads. ``list_card_templates`` is also the tool that
    proved the model-channel budget was each tool's discipline rather than a guard
    -- it shipped a 7 255-byte structuredContent against a 4 096-byte budget -- so
    it is exactly the kind of entry that must not reach a catalog by omission.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        list_card_templates,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_card,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
