"""The Context Hub MCP surface: events, knowledge, skills, and the hub read.

Nine tools that answer one question between them -- what does this Project know
-- and one private pair (`_discovery_scope`, `_discovery_summary`) that renders
their AD-1 text channel identically. The annotation family holds three of the
nine, and they are one gesture in three tenses: write it, correct it, withdraw
it. A surface that can only write is a surface whose mistakes are permanent.
The first seven moved here whole from `core.main`; nothing in them changed but
the address and the import of the entrypoint's own helpers.

THE `from core.main import ...` LINES INSIDE THE BODIES ARE LOAD-BEARING.
`core.main` imports this module, so a module-level import back would be a cycle
-- and, more importantly, `_resolve_project`, `_fetch_context_events`,
`_validate_event_input` and `get_access_token` are patched by the suites AT THE
`core.main` ADDRESS. A module that captured them at import time would ignore the
patch in silence and read production instead.
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import context_events as context_events_module


def add_context_event(
    project_id: str,
    event_date: str,
    type: str,
    label: str,
    description: str = "",
    metric: str | None = None,
) -> ToolResult:
    """Add a business context event for a project (Story 4.3, AC3).

    Persists a project-scoped annotation (sale, campaign launch, incident, etc.)
    to app.context_events so that analyses can reference known business events.

    AD-2: source-agnostic — no connector-specific strings.
    AD-10: this tool is the write path for the widget (callServerTool).
    HG-2: admin console uses the REST API; the widget uses this MCP tool.

    Story 9.10 (AC2): this tool STAYS model-visible (no AppConfig visibility,
    unlike submit_feedback) -- no widget calls it today (grep verified
    2026-07-14: ContextePanel uses the REST API; the HG-2 widget write path
    above describes an intent never built), and the model/chat is its real
    caller for "note this business event" requests.

    Parameters:
        project_id:   Project identifier.
        event_date:   ISO-8601 date string (YYYY-MM-DD).
        type:         Event type — free-form ('business', 'incident', 'deployment',
                      'release', ...). Not validated; Story 4.5 adds new types.
        label:        Short title, max 120 chars.
        description:  Optional free-form detail.
        metric:       Optional. WHICH governed metric this event is about, in the
                      vocabulary the analyses use ('cost', 'sessions', ...).
                      Leave it out when the event concerns every metric -- an
                      outage, a holiday, a site migration. Naming one narrows
                      where the event is offered as context: it is then attached
                      only to claims about that metric (migration 322). A metric
                      the Project does not govern is refused by name.
    """
    from core.main import (  # noqa: PLC0415
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    )
    created_by = identity or "anonymous"

    # Reject malformed input before project resolution touches the database.
    # ``persist_context_event`` validates again so direct callers keep the same
    # contract, while the MCP surface remains fail-fast and deterministic.
    context_events_module.validate_event_input(label, event_date)

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id, identity)

    # Story 53.1: an unscoped write here does not leak a row, it FABRICATES one
    # in someone else's project -- and what this table holds becomes the "Why"
    # of every narration built on top of it. Editing needs `edit`, not `view`.
    _refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    # Validate + persist + audit (AI-27: logic lives in core.context_events).
    # AC6 -- l'ecriture se fait sur une connexion qui porte deja le contexte
    # d'acces de l'appelant. `persist_context_event` ouvrait la sienne, nue :
    # armer celle du controle et laisser l'ecriture sur une autre est
    # exactement ce que `core/db.py` decrit comme n'achetant rien.
    from core import db as _core_db  # noqa: PLC0415

    with _core_db.request_connection(identity) as _write_conn:
        try:
            context_events_module.persist_context_event(
                project_id=project_id,
                event_date=event_date,
                type=type,
                label=label,
                description=description,
                created_by=created_by,
                metric=metric,
                conn=_write_conn,
            )
        except context_events_module.ContextEventRefusal as refusal:
            # This door speaks `ToolError` carrying `{code, message}`; the HTTP
            # door reads `status` and `payload` off the same exception. Rendering
            # it here is what `ContextEventRefusal`'s own docstring asks each door
            # to do -- and the message names the gesture, so a caller that typed
            # an ungoverned metric learns which one and where a new one is
            # declared.
            import json as _json  # noqa: PLC0415

            from fastmcp.exceptions import ToolError  # noqa: PLC0415

            raise ToolError(_json.dumps(refusal.payload)) from refusal

    # French-first write ack (UX-DR10). No structuredContent — write ack only (AD-1).
    # The ack NAMES the scope it wrote, because the scope changes where this
    # event will ever be offered as context: a caller that meant "every metric"
    # and got one, or the reverse, learns it here rather than from a "Why"
    # section that stays silent for a week.
    _scope = context_events_module.normalize_metric(metric)
    ack_text = f"Event added: {label} ({event_date})" + (
        f" — about {_scope}" if _scope else " — about every metric"
    )
    return ToolResult(
        content=[TextContent(type="text", text=ack_text)],
    )


def _annotation_refusal(refusal) -> None:
    """Render a `ContextEventRefusal` for the MCP door and raise it.

    The same payload the HTTP door renders from `status`/`payload`: one refusal,
    two doors, and the message names the gesture rather than the cause.
    """
    import json as _json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    raise ToolError(_json.dumps(refusal.payload)) from refusal


def correct_context_event(
    project_id: str,
    event_id: str,
    event_date: str | None = None,
    type: str | None = None,  # noqa: A002 -- the create door calls it `type`
    label: str | None = None,
    description: str | None = None,
    metric: str | None = None,
) -> ToolResult:
    """Correct a manual business context event a person or a model wrote before.

    THE SYMMETRIC DOOR of `add_context_event` (migration 286/328). Until it
    existed, an annotation dictated in chat -- "note that we changed the price on
    the 12th" -- could be written here and corrected only in the console, so the
    caller who mistyped the date had no way back through the surface that
    accepted it.

    A correction NEVER destroys the earlier wording: it files a revision holding
    the annotation as it read before, with the author and the day. The event's
    own id does not change, so a narration that cited it still resolves.

    Only MANUAL annotations. A connector-emitted event is derived from its
    Datastream's Event Configuration and re-emitted by the next pull, so a hand
    correction is a value the next ingest erases -- it is refused with the gesture
    that actually repairs. A WITHDRAWN annotation is refused too: it is the record
    of a withdrawal that already happened, and the repair is to write a new one.

    Parameters:
        project_id:  Project identifier.
        event_id:    The annotation to correct.
        event_date:  New business date (YYYY-MM-DD). Omit to leave it alone.
        type:        New type. Omit to leave it alone.
        label:       New short title, max 120 chars. Omit to leave it alone.
        description: New free-form detail -- the annotation's own words. Omit to
                     leave it alone; pass an empty string to clear it.
        metric:      WHICH governed metric the annotation is about. Omit to leave
                     it alone; pass an empty string to widen it back to every
                     metric. A metric the Project does not govern is refused by
                     name.

    At least one field must be named -- a correction that changes nothing is
    refused rather than acknowledged.
    """
    from core import db as _core_db  # noqa: PLC0415

    # THE TWO GATES, WRITTEN HERE AND NOT EXTRACTED, and that is a decision.
    # `tests/isolation/test_mcp_tool_scope_refusal.py` derives which TOOL calls
    # the scope seam by reading each tool's own body: a shared helper would make
    # this door invisible to that ratchet and pin the helper instead -- the same
    # "an import is not a guard" the file's header states. The gates are
    # `add_context_event`'s, for its reason: an unscoped write here does not leak
    # a row, it rewrites the "Why" of every narration built on this project.
    from core.main import (  # noqa: PLC0415
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    resolved = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(resolved, identity, minimum_capability="edit")

    # `UNSET` and `None` are different answers here, and the tool signature can
    # only carry one of them: an omitted parameter arrives as `None`, and the
    # service must read that as "leave it alone". Clearing a nullable field is
    # therefore an explicit empty string -- the one shape a JSON tool call can
    # tell apart from an omission.
    given: dict = {}
    for name, value in (
        ("event_date", event_date),
        ("type", type),
        ("label", label),
        ("description", description),
        ("metric", metric),
    ):
        if value is None:
            continue
        if value == "" and name in ("description", "metric"):
            given[name] = None
        else:
            given[name] = value

    if not given:
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        raise ToolError(
            '{"code": "no_change", "message": "Name at least one field to correct."}'
        )

    with _core_db.request_connection(identity) as _write_conn:
        try:
            corrected = context_events_module.update_manual_event(
                project_id=resolved,
                event_id=event_id,
                updated_by=identity,
                conn=_write_conn,
                **given,
            )
        except context_events_module.ContextEventRefusal as refusal:
            _annotation_refusal(refusal)

    # The ack NAMES what the annotation now says, not which fields moved: a
    # caller who mistyped one correction learns it here rather than from a
    # narration that stays wrong for a week.
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Event corrected: {corrected['label']} "
                    f"({corrected['event_date']})"
                    + (
                        f" — about {corrected['metric']}"
                        if corrected.get("metric")
                        else " — about every metric"
                    )
                ),
            )
        ],
    )


def withdraw_context_event(
    project_id: str,
    event_id: str,
    reason: str,
) -> ToolResult:
    """Withdraw a manual business context event -- it stops being read as a cause.

    A SUPERSEDE, NEVER A DELETE (migration 286). The annotation was a candidate
    cause in an anomaly alert, a marker on a card, a regressor in a feature
    matrix; destroying it would make those earlier answers unexplainable and lose
    the difference between "this was never said" and "this was said, and
    withdrawn". The row stays, carrying who withdrew it and why, and every
    proactive read stops serving it.

    A withdrawal is NEVER unmade. A caller who withdrew the wrong annotation
    writes the annotation again rather than resurrecting this one -- which is what
    the reason is for.

    Only MANUAL annotations: a connector-emitted event is stopped at its source,
    in that Datastream's Event Configuration.

    Parameters:
        project_id: Project identifier.
        event_id:   The annotation to withdraw.
        reason:     WHY it should stop being read, in the author's words.
                    Required: without it, the next reader cannot tell a wrong
                    annotation from an inconvenient one.
    """
    from core import db as _core_db  # noqa: PLC0415

    # THE TWO GATES, WRITTEN HERE AND NOT EXTRACTED, and that is a decision.
    # `tests/isolation/test_mcp_tool_scope_refusal.py` derives which TOOL calls
    # the scope seam by reading each tool's own body: a shared helper would make
    # this door invisible to that ratchet and pin the helper instead -- the same
    # "an import is not a guard" the file's header states. The gates are
    # `add_context_event`'s, for its reason: an unscoped write here does not leak
    # a row, it rewrites the "Why" of every narration built on this project.
    from core.main import (  # noqa: PLC0415
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    resolved = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(resolved, identity, minimum_capability="edit")

    with _core_db.request_connection(identity) as _write_conn:
        try:
            withdrawn = context_events_module.retire_manual_event(
                project_id=resolved,
                event_id=event_id,
                retired_by=identity,
                reason=reason,
                conn=_write_conn,
            )
        except context_events_module.ContextEventRefusal as refusal:
            _annotation_refusal(refusal)

    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Event withdrawn: {withdrawn['label']} "
                    f"({withdrawn['event_date']}) — it is no longer read as a "
                    f"cause. The row stays, carrying the reason."
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Story 31.5 — get_events: cross-source annotation read surface (AD-2 / AD-8).
#
# Returns project-scoped context events (manual + connector) enriched with
# dim_event_type fields (category, default_marker) for LLM filtering and the
# MMM substrat (events = regressors, value = intensity, platform = split;
# grain jour → pivot semaine/marché en aval).
#
# module_kind is NEVER exposed (epic §2: internal plumbing).
# AD-8: project_id is always resolved + scoped (never cross-project leak).
# AD-9: read-only; no metric value is modified.
# ---------------------------------------------------------------------------


def get_events(
    project_id: str,
    type: str | None = None,
    event_type: str | None = None,
    category: str | None = None,
    platform: str | None = None,
    source: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> ToolResult:
    """Return project-scoped context events with dim_event_type enrichment (Story 31.5).

    Source-agnostic read surface for events from ALL sources (manual + connectors).
    The ``module_kind`` of the Connector that emitted each event is NEVER exposed
    (epic §2: internal plumbing). Events are cross-source by design (project-scoped,
    not connection-scoped) so a ``video_upload`` from YouTube and a ``release`` from
    GitHub both appear here for the same project.

    Returned fields per event:
        event_date, event_type (alias: type), label, platform, source, value,
        category (from dim_event_type), default_marker (from dim_event_type).

    Filters (all optional, AND-combined):
        type / event_type  — filter by event_type string (both aliases accepted).
        category           — filter by dim_event_type.category
                             (content/engineering/marketing/commerce/business/operations).
        platform           — filter by platform (e.g. "youtube", "github").
        source             — filter by source ("manual" or connector name).
        from_date          — ISO-8601 start date (inclusive), default: 90 days ago.
        to_date            — ISO-8601 end date (inclusive), default: today.

    MMM substrat contract (grain jour → pivot semaine/marché en aval):
        - ``value`` is the optional event intensity / regressor magnitude (float | null).
          A null value signals a unit-pulse regressor (the event happened, no magnitude).
        - ``platform`` is the MMM market/channel split axis.
        - Combine ``get_events`` + ``fact_daily_kpi`` (via get_daily_report) to build a
          feature matrix: events = exogenous regressors, value = magnitude weight, date
          = join key. Adstock/decay and the model itself are out of scope here.
        - ``count: 0`` with ``events: []`` means the window was READ and holds zero
          live events. When the events could not be read at all the answer carries
          ``unavailable: {reason, repair}`` and neither ``count`` nor ``events`` --
          treat it as unknown, never as an empty window.

    AD-2: source-agnostic — no connector-specific strings in filters or output.
    AD-8: project_id is always resolved + scoped.
    AD-9: read-only; no metric value is modified.
    """
    from datetime import date, timedelta  # noqa: PLC0415

    from core.main import (  # noqa: PLC0415
        _fetch_context_events,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )
    from core.report_dictionary import enrich_events_with_dim  # noqa: PLC0415

    # Story 53.1: this tool never resolved an identity at all -- it took the
    # project id on trust. Both halves land together, or the check has nothing
    # to check against.
    #
    # 2026-08-25: the identity is resolved BEFORE the project, not after. The
    # resolver now decides existence for a NAMED caller (arbitration of
    # `mcp-tool-surface.md`), so reading the token afterwards would hand it a
    # caller it does not have and make the armed connection and the access
    # decision about two different people.
    _events_token: AccessToken | None = get_access_token()
    _events_identity = (
        (_events_token.claims.get("sub") or _events_token.client_id)
        if _events_token
        else "anonymous"
    ) or "anonymous"
    project_id = _resolve_project(project_id, _events_identity)
    _refuse_unless_project_scope(project_id, _events_identity)

    # Resolve date window.
    today = date.today().isoformat()
    _from = from_date or (date.today() - timedelta(days=90)).isoformat()
    _to = to_date or today

    import json as _json  # noqa: PLC0415

    # AI-344 (2026-09-01). This read served `count: 0, events: []` on a
    # deployment that keeps no DuckDB mirror while the record held six events.
    # The fetch now reads the record through the caller's scoped connection, and
    # when NEITHER store can serve it says so: no `count`, no `events` -- an MMM
    # feature-builder must not read "unavailable" as "no regressors".
    try:
        raw_events = _fetch_context_events(project_id, _from, _to, identity=_events_identity)
    except context_events_module.ContextEventsUnavailable as exc:
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=_json.dumps(
                        {
                            "project_id": project_id,
                            "from": _from,
                            "to": _to,
                            "unavailable": exc.payload,
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )

    # Resolve the effective type filter (accept both aliases: type and event_type).
    type_filter: str | None = (type or event_type or "").strip() or None

    # Enrich with dim_event_type (category, default_marker).
    enriched = enrich_events_with_dim(raw_events)

    # Apply optional AND filters.
    results: list[dict] = []
    for e in enriched:
        if type_filter and e.get("type") != type_filter:
            continue
        if category and e.get("category") != category:
            continue
        if platform and e.get("platform") != platform:
            continue
        if source and e.get("source") != source:
            continue
        results.append(
            {
                "event_date": e.get("event_date"),
                "event_type": e.get("type"),  # canonical field name (alias exposed)
                "label": e.get("label"),
                "platform": e.get("platform"),
                "source": e.get("source"),
                "value": e.get("value"),
                "category": e.get("category", ""),
                "default_marker": e.get("default_marker", "pin"),
            }
        )

    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=_json.dumps(
                    {
                        "project_id": project_id,
                        "from": _from,
                        "to": _to,
                        "count": len(results),
                        "events": results,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Context Hub, Knowledge & Skills MCP Read/Write Surfaces
# ---------------------------------------------------------------------------

#: AD-1 line budget shared by the three Context Hub discovery tools. Same
#: number, same reason, as _SEARCH_CONTEXT_MAX_LINES: the LLM channel carries a
#: readable index, never the corpus.
_DISCOVERY_MAX_LINES = 30


def _discovery_scope(row: dict) -> str:
    """Return 'platform' or 'project' for one row -- the scope AD-5 turns on."""
    return "platform" if row.get("project_id") is None else "project"


def _discovery_summary(header: str, lines: list[str]) -> str:
    """One header + a capped body, with the truncation SAID (AD-1 / NFR1).

    The three discovery tools each serialized their whole payload into
    TextContent: every topic body, every procedure body, the full taxonomy.
    A project with fifty governed topics burned the caller's context window on
    a call whose purpose is to say WHAT EXISTS. The detail did not disappear --
    it moved to structuredContent, which is the channel that was built for it.
    """
    budget = _DISCOVERY_MAX_LINES - 1  # reserve the truncation marker
    out = [header]
    for line in lines:
        if len(out) >= budget:
            break
        out.append(line)
    remaining = len(lines) - (len(out) - 1)
    if remaining > 0:
        out.append(f"[+{remaining} more in the detail]")
    return "\n".join(out[:_DISCOVERY_MAX_LINES])


def get_knowledge(
    project_id: str,
    topic_id: str | None = None,
    query: str | None = None,
) -> ToolResult:
    """Return governed knowledge topics (Knowledge Library) -- index + detail (AD-1).

    Reads platform-scoped and project-scoped knowledge entries from
    app.context_topics. The LLM channel gets a <=30-line index (one line per
    topic: title, scope, version); the bodies ride structuredContent.

    Parameters:
        project_id: Project identifier.
        topic_id:   Optional topic ID to fetch a specific topic.
        query:      Optional search query to filter topics by title or body text.
    """
    from core import context_store
    from core import db as core_db
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity)

    with core_db.request_connection(identity) as conn:
        if topic_id:
            # ⚠️ `get_topic_by_id` N'A JAMAIS EXISTE. Mesure du 2026-08-10 :
            #     python -c "from core import context_store;
            #                print(hasattr(context_store,'get_topic_by_id'))"
            #       -> False
            # Chaque appel a `get_knowledge(topic_id=...)` levait donc un
            # `AttributeError`, et rien ne parcourait cette branche. Le lecteur
            # EXISTE et s'appelle `get_topic` : il porte deja le refus de portee
            # (AD-5) et ses tests. En ecrire un second serait deux lecteurs de la
            # meme ligne -- le defaut que cette vague ferme partout ailleurs.
            topic = context_store.get_topic(
                conn, topic_id=topic_id, caller_project_id=project_id
            )
            topics = [topic] if topic else []
        else:
            topics = context_store.list_topics(conn, project_id=project_id)

    if query:
        q = query.strip().lower()
        topics = [
            t
            for t in topics
            if q in (t.get("title") or "").lower()
            or q in (t.get("body_md") or "").lower()
        ]

    # AD-18, extended 2026-09-01 (`analyze-and-test.md`, amendment of that date):
    # `get_knowledge` is the second call of every observed Analyze session and
    # the gate did not count it, so a caller who did exactly what the Skill said
    # was measured non-adherent. Same rule as `search_context` [F-1]: only a read
    # that returned at least one topic opens the exchange -- a library that
    # answered nothing informed nothing. Best-effort; never breaks the read.
    if topics:
        try:
            from core.agent_surface_mcp import _mark_context_consulted  # noqa: PLC0415

            _mark_context_consulted("get_knowledge", project_id)
        except Exception as _mark_exc:  # noqa: BLE001 -- observation, never enforcement
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).debug(
                "get_knowledge: context_mark_skipped: %s", _mark_exc
            )

    summary = _discovery_summary(
        f"Knowledge in '{project_id}' — {len(topics)} topic(s):"
        if topics
        else f"No governed knowledge topic in '{project_id}'.",
        [
            f"- {' '.join((t.get('title') or '').split())}"
            f" [{_discovery_scope(t)}, v{t.get('version_number')}] ({t.get('id')})"
            for t in topics
        ],
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(
            {
                "project_id": project_id,
                "identity": identity,
                "count": len(topics),
                "topics": topics,
            },
            provenance={
                "source_system": "context-store",
                "source_field": "get_knowledge",
                "pull_id": None,
            },
            freshness="live",
        ),
    )


def add_knowledge(
    project_id: str,
    title: str,
    body_md: str,
    owner: str | None = None,
) -> ToolResult:
    """Add a governed knowledge entry (topic) to the Knowledge Library.

    Parameters:
        project_id: Project identifier.
        title:      Title of the knowledge entry.
        body_md:    Markdown body content.
        owner:      Optional owner (email or username).
    """
    from core import context_store
    from core import db as core_db
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    with core_db.request_connection(identity) as conn:
        topic = context_store.create_topic(
            conn,
            project_id=project_id,
            title=title,
            body_md=body_md,
            owner=owner,
            created_by=identity,
        )
        conn.commit()

    ack_text = f"Knowledge topic added: {title} (id: {topic.get('id')})"
    return ToolResult(
        content=[TextContent(type="text", text=ack_text)],
        # L'id dans la SEULE phrase texte forçait l'agent a parser une phrase
        # pour reference la carte qu'il venait d'ecrire (constat E2E O2).
        structured_content=_envelope(
            {
                "id": topic.get("id"),
                "title": title,
                "project_id": project_id,
                "version_number": topic.get("version_number"),
            },
            provenance={
                "source_system": "context-store",
                "source_field": "add_knowledge",
                "pull_id": None,
            },
            freshness="live",
        ),
    )


def get_skills(
    project_id: str,
    procedure_id: str | None = None,
    query: str | None = None,
) -> ToolResult:
    """Return governed agent Skills (Skills Registry) -- index + detail (AD-1).

    Reads platform-scoped and project-scoped agent procedures from app.procedures.
    The LLM channel gets a <=30-line index (name, description, scope, version);
    the frontmatter and bodies ride structuredContent.

    Parameters:
        project_id:   Project identifier.
        procedure_id: Optional procedure ID to fetch a specific skill/procedure.
        query:        Optional search query to filter procedures by name, description or body.
    """
    from core import context_store
    from core import db as core_db
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity)

    with core_db.request_connection(identity) as conn:
        if procedure_id:
            # Meme defaut, meme reparation : `get_procedure_by_id` n'existe pas
            # davantage, et `get_procedure` est le lecteur scope qui existe --
            # `caller_project_id`, pas `project_id`. Un id d'un projet voisin
            # rend `None`, jamais la ligne ; une Skill de PLATEFORME
            # (`project_id IS NULL`) reste lisible par tous, ce qui est la regle
            # AD-5 et non une exception.
            proc = context_store.get_procedure(
                conn, procedure_id=procedure_id, caller_project_id=project_id
            )
            procedures = [proc] if proc else []
        else:
            procedures = context_store.list_procedures(conn, project_id=project_id)

    if query:
        q = query.strip().lower()
        procedures = [
            p
            for p in procedures
            if q in (p.get("name") or "").lower()
            or q in (p.get("description") or "").lower()
            or q in (p.get("body_md") or "").lower()
        ]

    summary = _discovery_summary(
        f"Skills in '{project_id}' — {len(procedures)} skill(s):"
        if procedures
        else f"No governed skill in '{project_id}'.",
        [
            f"- {' '.join((p.get('name') or '').split())}"
            f" [{_discovery_scope(p)}, v{p.get('version_number')}]"
            f" — {' '.join((p.get('description') or '').split())[:120]}"
            for p in procedures
        ],
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(
            {
                "project_id": project_id,
                "identity": identity,
                "count": len(procedures),
                "procedures": procedures,
            },
            provenance={
                "source_system": "context-store",
                "source_field": "get_skills",
                "pull_id": None,
            },
            freshness="live",
        ),
    )


def add_skill(
    project_id: str,
    name: str,
    description: str,
    body_md: str,
    owner: str | None = None,
) -> ToolResult:
    """Add a governed agent procedure/skill to the Skills Registry.

    Parameters:
        project_id:  Project identifier.
        name:        Skill / procedure name (kebab-case or title).
        description: Short summary of what this skill accomplishes.
        body_md:     Detailed Markdown instructions or steps.
        owner:       Optional owner (email or username).
    """
    import yaml

    from core import context_store
    from core import db as core_db
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity, minimum_capability="edit")

    fm_dict = {"name": name, "description": description}
    if owner:
        fm_dict["owner"] = owner
    fm_yaml = yaml.safe_dump(fm_dict, sort_keys=False)

    with core_db.request_connection(identity) as conn:
        proc = context_store.create_procedure(
            conn,
            project_id=project_id,
            frontmatter_yaml=fm_yaml,
            body_md=body_md,
            owner=owner,
            created_by=identity,
        )
        conn.commit()

    ack_text = f"Skill procedure added: {name} (id: {proc.get('id')})"
    return ToolResult(
        content=[TextContent(type="text", text=ack_text)],
        # Meme contrat qu'add_knowledge : l'id est une donnee, pas une phrase.
        structured_content=_envelope(
            {
                "id": proc.get("id"),
                "name": name,
                "project_id": project_id,
                "version_number": proc.get("version_number"),
            },
            provenance={
                "source_system": "context-store",
                "source_field": "add_skill",
                "pull_id": None,
            },
            freshness="live",
        ),
    )


def get_context_hub(
    project_id: str,
) -> ToolResult:
    """Aggregated Context Hub overview -- counts on the LLM channel, corpus in detail (AD-1).

    The LLM channel carries a <=30-line census (domains, classifications, links,
    knowledge, skills, plus the first business domains by name); the taxonomy,
    the topics and the procedures ride structuredContent.

    Parameters:
        project_id: Project identifier.
    """
    from core import business_taxonomy, context_store
    from core import db as core_db
    from core.context_api import _project_org_id  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _envelope,
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    ) or "anonymous"
    project_id = _resolve_project(project_id, identity)
    _refuse_unless_project_scope(project_id, identity)

    with core_db.request_connection(identity) as conn:
        topics = context_store.list_topics(conn, project_id=project_id)
        procedures = context_store.list_procedures(conn, project_id=project_id)
        # The org is the PROJECT's org. `org_id="org_default"` was hard-coded
        # here, so every project outside that org read a taxonomy and a link set
        # that were not its own -- an AD-5 scope break, not a default.
        org_id = _project_org_id(conn, project_id)
        taxonomy = business_taxonomy.list_taxonomy(conn, org_id=org_id)
        links = business_taxonomy.list_links(conn, org_id=org_id, project_id=project_id)

    domains = taxonomy.get("domains", [])
    classifications = taxonomy.get("classifications", [])
    summary = _discovery_summary(
        f"Context Hub of '{project_id}': {len(domains)} business domain(s),"
        f" {len(classifications)} classification(s), {len(links)} governed link(s),"
        f" {len(topics)} knowledge topic(s), {len(procedures)} skill(s).",
        [f"- domain: {' '.join((d.get('name') or '').split())} ({d.get('slug')})" for d in domains],
    )
    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(
            {
                "project_id": project_id,
                "org_id": org_id,
                "identity": identity,
                "domains_count": len(domains),
                "classifications_count": len(classifications),
                "links_count": len(links),
                "knowledge_topics_count": len(topics),
                "skills_count": len(procedures),
                "taxonomy": taxonomy,
                "links": links,
                "topics": topics,
                "procedures": procedures,
            },
            provenance={
                "source_system": "context-store",
                "source_field": "get_context_hub",
                "pull_id": None,
            },
            freshness="live",
        ),
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the nine Context Hub tools on the given FastMCP app (declared -- AD-43).

    Four reads and five writes, and the split is the point. `context-hub.md` is
    the page that already said "the discovery tools must not spend the caller's
    context window on the corpus they were asked to index"; until 2026-08-12 all
    seven of its tools reached the catalog with no declaration at all, so the three
    that ADD to the governed corpus -- an event, a knowledge entry, a Skill -- sat
    in the default Insights catalog of every host that ever connected.

    They leave it. The corpus of governed business knowledge is what the product
    reads before it answers; a model writing into it unprompted is the one thing
    the Context Hub cannot afford. The Console keeps every one of these writes.

    TWO OF THE FIVE WRITES ARE NEW, and they are the symmetric door of the third
    (migration 328, Jean's decision of 2026-08-31). `add_context_event` could
    write an annotation from chat and nothing here could correct or withdraw one:
    the surface that ACCEPTED the wrong date was not the surface that could repair
    it, so the caller had to be told to open a console they may not have. They
    carry the same profile, the same effect and the same human confirmation as the
    door they mirror -- a withdrawal is not a lesser act than a creation.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_events,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_knowledge,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_skills,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_context_hub,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        add_context_event,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        correct_context_event,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        withdraw_context_event,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        add_knowledge,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        add_skill,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
