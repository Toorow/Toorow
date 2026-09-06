"""toorow -- context-event helpers (Story 4.3; extracted 5.1/AI-27).

Extracted verbatim from ``core.main`` in Story 5.1 (AI-27 decomposition; Story 4.3
AC7 anticipated this move with a "TODO: extract _fetch_context_events" note). Pure
behaviour-preserving move: ``main.py`` re-exports ``_fetch_context_events`` and
``_validate_event_input`` for backward compatibility with existing imports/tests.

The fetch serves from the DuckDB mirror (``mirror.context_events``) when the
deployment keeps one, else from the system of record (``app.context_events``),
and says ``unavailable`` when neither can serve -- context-hub.md, amendment of
2026-09-01 (AI-344). The earlier "AD-12 / NFR6: the mirror is the single
analytical read path" note was a Story 4.4 architecture note that no ratified
document carries; on a deployment with no mirror it made every reader of this
function serve "this project observed nothing".

AD-2: source-agnostic -- no module-specific strings.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re

from core.audit import declare_action

logger = logging.getLogger(__name__)

# The two halves of the human lifecycle migration 286 opened. They are declared
# HERE and not in `core/audit.py` because this module is their only writer --
# `context_events_api` delegates both gestures to the functions below rather than
# writing its own SQL, which is the rule `core/audit.py` states and
# `tests/conformance/test_audit_actions_are_declared_by_their_writer.py` enforces.
# `context_event.created` stays central precisely because it has two writers.
ACTION_CONTEXT_EVENT_UPDATED = declare_action("context_event.updated")
ACTION_CONTEXT_EVENT_RETIRED = declare_action("context_event.retired")


class ContextEventRefusal(Exception):
    """A refusal a caller can act on, on both doors.

    The MCP path of this module raises ``fastmcp.exceptions.ToolError`` carrying a
    JSON ``{code, message}``; the HTTP path needs a status code and a plain
    envelope. Bending ``ToolError`` into HTTP would put a tool-transport concept
    in a route, so the refusal is its own type and each door renders it: the route
    reads ``status`` and ``payload``, an MCP caller would wrap ``payload`` in a
    ``ToolError`` exactly as the validators below already do.

    ``message`` names the GESTURE that repairs, never the technical cause --
    CLAUDE.md, and the reason a connector-sourced row is refused at all.
    """

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    @property
    def payload(self) -> dict:
        return {"code": self.code, "message": self.message}


class ContextEventsUnavailable(RuntimeError):
    """A context-event read could not be served from ANY store -- said, not zeroed.

    ``fetch_context_events`` used to answer ``[]`` whenever the DuckDB mirror was
    absent, and on a deployment that keeps no mirror that is every call: measured
    2026-09-01, ``get_events`` served ``count: 0`` for a project holding six live
    events. An empty list now means one thing only -- a store was read and the
    window holds zero live events -- and every other state is this exception.

    ``reason`` says what could not be read; ``repair`` names the GESTURE that
    opens the read again, never the technical cause alone (CLAUDE.md). Each door
    renders ``payload`` in its own vocabulary: ``get_events`` puts it under
    ``unavailable``, an envelope puts it beside ``meta.context_events``.
    """

    def __init__(self, reason: str, repair: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.repair = repair

    @property
    def payload(self) -> dict:
        return {"reason": self.reason, "repair": self.repair}


class _Unset:
    """"Not given", which is NOT ``None``.

    On the create door every field arrives at once, so ``None`` unambiguously
    means "absent". On a correction it cannot: clearing a wrong ``platform``, a
    wrong ``value`` or a wrong entity reference is exactly the repair someone
    needs, and a correction that could not write ``NULL`` would leave the very
    fields most often mistyped uncorrectable.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return "<unset>"


#: The sentinel callers pass nothing instead of. Public: the HTTP route builds its
#: keyword arguments from the fields the JSON body actually carried.
UNSET = _Unset()

#: WHICH Datastream is collecting right now. A pull signature carries the
#: Connector's arguments, never the Datastream -- so the writer could only guess
#: from the Connector's name, and two event feeds of one Connector in a Project
#: made that guess impossible. The drivers know, and say so here.
_COLLECTING_DATASTREAM: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "toorow_collecting_datastream", default=None
)


@contextlib.contextmanager
def collecting_for_datastream(datastream_id: str | None):
    """Name the Datastream every event persisted inside this scope belongs to."""
    token = _COLLECTING_DATASTREAM.set(str(datastream_id) if datastream_id else None)
    try:
        yield
    finally:
        _COLLECTING_DATASTREAM.reset(token)


def _active_event_binding(conn, *, project_id: str, source: str) -> tuple[str, str] | None:
    """The Datastream and Event Configuration version this observation belongs to.

    `app.require_event_observation_binding` refuses a row carrying neither, and
    this INSERT carried neither -- so no Connector event could ever be written,
    for any Connector: measured 2026-08-12, a report that had just returned 5
    publications from the provider died here on `db_error`. The trigger is right;
    the writer had simply never been told about it.

    The collecting Datastream answers when a driver named it -- it always knows,
    and a pull signature never carries it. Otherwise the Project and `source`
    (the Connector's name) answer, provided exactly one active configuration
    matches. TWO ARE NOT GUESSED BETWEEN: the row goes in unbound and the
    trigger refuses it, which is a refusal someone can act on, unlike a silently
    wrong attribution. A manual event has no Connector and stays unbound.
    """
    if not source or source == "manual":
        return None
    collecting = _COLLECTING_DATASTREAM.get()
    try:
        with conn.cursor() as cur:
            if collecting:
                # A NAMED Datastream needs no Connector to be found by (Story
                # 68.4). The `module_name = source` join exists to disambiguate
                # when nobody said which stream is collecting; a managed feed
                # carries `module_name IS NULL`, so that join could never match
                # and every imported event would go in unbound -- refused by the
                # trigger, for a stream whose identity was never in doubt.
                cur.execute(
                    """
                    SELECT v.datastream_id, v.id
                      FROM app.event_configuration_versions v
                      JOIN app.datastreams d
                        ON d.id = v.datastream_id AND d.project_id = v.project_id
                     WHERE v.project_id = %s AND v.review_state = 'active'
                       AND d.archived_at IS NULL
                       AND v.datastream_id = %s
                    """,
                    (project_id, collecting),
                )
            else:
                cur.execute(
                    """
                    SELECT v.datastream_id, v.id
                      FROM app.event_configuration_versions v
                      JOIN app.datastreams d
                        ON d.id = v.datastream_id AND d.project_id = v.project_id
                     WHERE v.project_id = %s AND v.review_state = 'active'
                       AND d.module_name = %s AND d.archived_at IS NULL
                    """,
                    (project_id, source),
                )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- the trigger stays the authority
        logger.warning("context_events: event_binding_unresolved source=%s: %s", source, exc)
        return None
    if len(rows) != 1:
        logger.warning(
            "context_events: event_binding_ambiguous source=%s project=%s matches=%d",
            source,
            project_id,
            len(rows),
        )
        return None
    return str(rows[0][0]), str(rows[0][1])


def normalize_entity_pair(
    entity_key, entity_kind, *, source: str, gesture: str
) -> tuple[str | None, str | None]:
    """WHICH ENTITY THIS EVENT IS ABOUT, normalized once for every door.

    Either both halves or neither -- migration 262 CHECKs the same rule, and a key
    with no kind is a key nothing can compare safely. A half-filled pair is
    dropped to NULL rather than refused: an event that names its entity badly is
    still a real marker of a real day, and losing the day to keep the key would be
    the worse trade.

    It is a function and not two copies because the correction door resolves the
    pair against what the row ALREADY carries -- a caller may correct the key
    alone -- so the two doors would otherwise apply the rule to different inputs
    and only one of them would agree with the CHECK.
    """
    key_clean = str(entity_key).strip() if entity_key else None
    kind_clean = str(entity_kind).strip() if entity_kind else None
    if not (key_clean and kind_clean):
        if key_clean or kind_clean:
            logger.warning(
                "%s: half an entity reference (key=%r kind=%r) on source=%s "
                "-- dropped, the event is kept",
                gesture, key_clean, kind_clean, source,
            )
        key_clean = kind_clean = None
    return key_clean, kind_clean


def normalize_metric(metric) -> str | None:
    """WHICH METRIC THIS EVENT IS ABOUT, normalized once for every door.

    Migration 322. ``None``, an empty string and whitespace all mean *about no
    metric in particular* -- an outage, a holiday, a site migration concerns
    every metric, and such an event stays admissible under every claim. That is
    the only absence there is: the CHECK of migration 322 refuses a blank name,
    because a blank reads as a metric nobody can compare and would silently
    disqualify the event under every claim instead of none.

    Shape only. Whether the NAME is one the product governs is
    :func:`assert_metric_is_governed`, which needs a connection -- the same split
    `context_store.validate_procedure_frontmatter` / `assert_mdm_tags_resolve`
    already makes, and for the same reason: the doors that have no connection
    (a connector transform, a preview capture) still get the shape right.
    """
    if metric is None:
        return None
    clean = str(metric).strip()
    return clean or None


def assert_metric_is_governed(
    conn, metric: str | None, project_id: str | None = None
) -> None:
    """A named metric is one the product GOVERNS -- otherwise it compares to nothing.

    ONE VOCABULARY, or the comparison this column exists for cannot happen. The
    claims say ``cost`` because ``fact_daily_kpi.metric`` says ``cost``, and
    ``app.target_fields`` and the published Semantic Concepts spell it the same
    way. An event that said ``Ad spend`` would name a metric that matches no
    claim ever, disqualify itself everywhere, and never say why -- which is worse
    than the unscoped state this column replaces.

    The catalogue is exactly the one `context_store.assert_mdm_tags_resolve`
    reads for a Skill's `mdm_tags` (`core.governed_field_catalogue
    .governed_names`: Semantic Model first, legacy dictionary in fallback), so a
    metric a person can PICK in the console is a metric this door accepts.

    The refusal names the gesture, never the technical cause: the metric that is
    not governed, and the three ways out of it.
    """
    if not metric:
        return
    from core.governed_field_catalogue import governed_names  # noqa: PLC0415

    if metric in governed_names(conn, names=[metric], project_id=project_id):
        return
    raise ContextEventRefusal(
        "metric_not_governed",
        f"'{metric}' is not a metric this Project governs. Pick one of its "
        "governed metrics, leave the metric empty when the event concerns every "
        "metric, or declare it as a Concept in the Semantic Model.",
        status=422,
    )


def persist_context_event(
    *,
    project_id: str,
    event_date: str,
    type: str,
    label: str,
    description: str,
    created_by: str,
    platform: str | None = None,
    value: float | None = None,
    source: str = "manual",
    entity_key: str | None = None,
    entity_kind: str | None = None,
    metric: str | None = None,
    conn=None,
    execution_id: str | None = None,
    commit: bool = True,
) -> str:
    """Validate + persist one context event to app.context_events; return its id.

    Extracted from ``core.main.add_context_event`` in Story 5.1 (AI-27). Mints a
    prefixed ULID (``evt_``), inserts the row, and writes the audit row (never
    raises -- AC6 in audit.py). Raises ``fastmcp.exceptions.ToolError`` on invalid
    input or a DB insert error, with a French-first message (UX-DR10).

    AD-2: source-agnostic -- no module-specific strings.

    ``conn`` -- story 53.1, AC6. The MCP tool hands in a connection ALREADY
    carrying the caller's access context (``core.db.request_connection``), so the
    row-level floor is the second barrier under the scope check the tool just
    passed. It is an argument and not the default because the other two callers
    are connector pulls (``modules/brevo``, ``modules/github``): their
    ``created_by`` is a connector name, not a person, and arming a connection for
    an identity that has no ``app.org_members`` row would refuse every ingest.
    Those paths keep the unarmed acquisition, and it is a decision written here
    rather than an omission. When ``conn`` is given the caller OWNS it: this
    function commits on it and never closes it.

    ``execution_id`` -- Story 68.4. WHICH run put this marker on the calendar.
    The column has existed since migration 133 and no writer ever filled it, so
    an event could not name the run that produced it while every fact could
    (AD-7). A file import passes the execution its ledger minted.

    ``commit`` -- Story 68.4. An import owns its transaction: its events, its
    ledger row and its execution transition land together or not at all, and a
    commit in the middle would leave events behind a refused import. Pass
    ``False`` with a ``conn`` and the caller commits; the audit row then rides
    the same transaction instead of opening its own.
    """
    from contextlib import nullcontext  # noqa: PLC0415

    from ulid import ULID  # noqa: PLC0415

    # A PREVIEW WRITES NOWHERE, and a context Connector writes HERE rather than
    # into the warehouse -- so the raw-landing capture cannot cover it and this
    # is the second door that has to honour the same scope. Without it a preview
    # of a context module (github today, every future one) would persist real
    # rows into `app.context_events` before anybody confirmed the Datastream.
    from core.raw_landing import active_preview_capture  # noqa: PLC0415

    entity_key_clean, entity_kind_clean = normalize_entity_pair(
        entity_key, entity_kind, source=source, gesture="persist_context_event"
    )
    # Migration 322: WHICH metric this event is about, or None for "every metric".
    # The shape is settled before the preview branch so a preview capture records
    # exactly what the real write would store; the catalogue check runs below, on
    # the connection the write itself uses.
    metric_clean = normalize_metric(metric)

    capture = active_preview_capture()
    if capture is not None:
        event_id = f"evt_{ULID()}"
        capture["rows"].append({
            "id": event_id,
            "project_id": project_id,
            "event_date": event_date,
            "type": type,
            "label": label,
            "description": description,
            "platform": platform,
            "value": value,
            "source": source,
            "entity_key": entity_key_clean,
            "entity_kind": entity_kind_clean,
            "metric": metric_clean,
        })
        if "app.context_events" not in capture["tables"]:
            capture["tables"].append("app.context_events")
        return event_id

    from core.audit import ACTION_CONTEXT_EVENT_CREATED, write_audit_row  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    # Validate inputs (shared with REST endpoint)
    validate_event_input(label, event_date)
    # Epic 31: enforce the canonical event vocabulary for connector events.
    validate_event_type(type, source)

    # Mint prefixed ULID (AD-14 / ARCHITECTURE-SPINE §IDs: evt_)
    evt_id = f"evt_{ULID()}"

    try:
        with (nullcontext(conn) if conn is not None else get_connection()) as conn:
            assert_metric_is_governed(conn, metric_clean, project_id)
            binding = _active_event_binding(conn, project_id=project_id, source=source)
            with conn.cursor() as cur:
                # Epic 31: platform/value/source are additive nullable columns
                # (migration 055). platform = level 1 of platform>type>label,
                # value = optional MMM regressor magnitude, source = manual|<connector>.
                cur.execute(
                    """
                    INSERT INTO app.context_events
                        (id, project_id, event_date, type, label, description,
                         created_by, platform, value, source,
                         datastream_id, event_configuration_version_id,
                         entity_key, entity_kind, execution_id, metric)
                    VALUES (%s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    """,
                    (
                        evt_id, project_id, event_date, type, label, description or None,
                        created_by, platform, value, source,
                        binding[0] if binding else None,
                        binding[1] if binding else None,
                        entity_key_clean, entity_kind_clean, execution_id,
                        metric_clean,
                    ),
                )
            if commit:
                conn.commit()
    except ContextEventRefusal:
        # A refusal is not a database failure and must not be dressed as one:
        # `metric_not_governed` names the gesture that repairs, and wrapping it
        # in `db_error` would send a person to look at the server instead of at
        # their own metric.
        raise
    except Exception as exc:
        logger.error("add_context_event: db_insert_error: %s", exc)
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database error: {exc}"})
        ) from exc

    metadata = {
        "event_id": evt_id,
        "project_id": project_id,
        "type": type,
        "label": label,
    }
    if execution_id:
        metadata["execution_id"] = execution_id
    if commit:
        # Write audit row (never raises — AC6 in audit.py)
        write_audit_row(
            identity=created_by,
            action=ACTION_CONTEXT_EVENT_CREATED,
            provider_account="",
            connection_ref="",
            metadata=metadata,
        )
    else:
        # The caller owns the transaction, so the evidence rides it: an audit
        # row committed by its own connection would survive an import that
        # rolled its events back, and claim a write that never happened.
        from core.audit import insert_audit_row  # noqa: PLC0415

        insert_audit_row(
            conn,
            identity=created_by,
            action=ACTION_CONTEXT_EVENT_CREATED,
            provider_account="",
            connection_ref="",
            metadata=metadata,
        )
    return evt_id


#: What a correction or a retirement hands back. One list, so the two write doors
#: and the console render the same event -- a shape that differs between the read
#: and the answer to a write is a shape the screen has to reconcile by guessing.
_EVENT_ROW_COLS = (
    "id", "project_id", "event_date", "type", "label", "description",
    "created_by", "created_at", "platform", "value", "source",
    "entity_key", "entity_kind", "metric", "retired_at", "retired_by",
    "retired_reason",
)

#: The fields a human may correct. `source`, `created_by`, `created_at` and the
#: binding columns are NOT here: they say where the event came from, and an origin
#: that can be rewritten is an origin that proves nothing (migration 212).
_CORRECTABLE_COLUMNS = (
    "event_date", "type", "label", "description", "platform", "value",
    # Migration 322. Correctable for the same reason `platform` is: naming the
    # WRONG metric is how an annotation silently stops being attached to the
    # claim it explains, and the only visible symptom is a "Why" that goes quiet.
    "metric",
)

#: What a revision keeps of the annotation it superseded, as
#: ``column on app.context_events`` -> ``column on app.context_event_revisions``.
#: Migration 328. The set is `_CORRECTABLE_COLUMNS` plus the entity pair: exactly
#: what a human hand can move, because a column no correction can touch is a
#: column a revision would repeat forever without ever differing.
_REVISION_COLUMNS = {
    "event_date": "previous_event_date",
    "type": "previous_type",
    "label": "previous_label",
    "description": "previous_description",
    "platform": "previous_platform",
    "value": "previous_value",
    "metric": "previous_metric",
    "entity_key": "previous_entity_key",
    "entity_kind": "previous_entity_kind",
}

#: One revision, in the vocabulary the screen reads. `previous_` is dropped: the
#: row already SAYS it is a previous wording, and a screen that renders
#: `previous_label` under a heading that reads "previous wording" says it twice.
_REVISION_ROW_COLS = (
    "id", "event_id", "project_id", "revision_no", "superseded_at",
    "corrected_by", "corrected_fields",
    *_REVISION_COLUMNS.values(),
)

#: Each refusal names the gesture that repairs, and the gesture differs by what
#: the caller was trying to do -- "correct it over there" and "stop it at the
#: source" are not the same sentence, and a single generic one would send half the
#: callers to the wrong screen.
_REFUSAL_SENTENCES = {
    "correct": {
        "connector_owned": (
            "This event was emitted by the {source} connector. Correct it in that "
            "Datastream's Event Configuration, not here."
        ),
        "already_retired": (
            "This event was retired on {retired_at}. Write a new event instead of "
            "correcting a retired one."
        ),
    },
    "retire": {
        "connector_owned": (
            "This event was emitted by the {source} connector. Stop it at the "
            "source, in that Datastream's Event Configuration, not here."
        ),
        "already_retired": (
            "This event was already retired on {retired_at}, by {retired_by}."
        ),
    },
}


def _day(value) -> str:
    """A date or timestamp as the day a person would say out loud."""
    return str(value)[:10] if value is not None else ""


def serialize_event_row(cols, row) -> dict:
    """One event, in the vocabulary the screen and the tool both read.

    ``value`` is NUMERIC in Postgres and arrives as ``Decimal``, which no JSON
    encoder in this repository accepts; dates and timestamps arrive as objects.
    Both are converted the way ``_list_context_events`` already converts them, so
    a corrected event and a listed event are the same record.
    """
    record: dict = {}
    for col, val in zip(cols, row):
        if val is None:
            record[col] = None
        elif col in ("created_at", "retired_at"):
            record[col] = val.isoformat()
        elif col == "event_date":
            record[col] = str(val)
        elif col == "value":
            record[col] = float(val)
        else:
            record[col] = val
    return record


def _locked_manual_event(conn, *, project_id: str, event_id: str, gesture: str) -> dict:
    """The row a human write is about to touch, or the refusal that names the gesture.

    ``FOR UPDATE``: the decision (is this manual? is it still live?) and the write
    that follows it must not be separable. Two callers retiring the same event, or
    a connector re-ingest landing between the read and the UPDATE, would otherwise
    each act on a state that no longer holds.

    Scoped by ``project_id`` as well as by id, and an event of ANOTHER project
    answers exactly as a nonexistent one does -- an id that exists somewhere is
    not something a caller may learn by trying (AD-8, and the same non-disclosing
    404 `_context_event_not_found` already serves).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, retired_at, retired_by, event_date, type, label,
                   description, platform, value, entity_key, entity_kind, metric
              FROM app.context_events
             WHERE id = %s AND project_id = %s
             FOR UPDATE
            """,
            (event_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ContextEventRefusal(
                "not_found", "Context event not found", status=404
            )
        cols = [desc[0] for desc in cur.description]
    current = dict(zip(cols, row))

    source = current.get("source") or "manual"
    if source != "manual":
        raise ContextEventRefusal(
            "connector_owned",
            _REFUSAL_SENTENCES[gesture]["connector_owned"].format(source=source),
        )
    if current.get("retired_at") is not None:
        raise ContextEventRefusal(
            "already_retired",
            _REFUSAL_SENTENCES[gesture]["already_retired"].format(
                retired_at=_day(current["retired_at"]),
                retired_by=current.get("retired_by") or "",
            ),
        )
    return current


def update_manual_event(
    *,
    project_id: str,
    event_id: str,
    updated_by: str,
    event_date=UNSET,
    type=UNSET,  # noqa: A002 -- the create door calls it `type`; one word per notion
    label=UNSET,
    description=UNSET,
    platform=UNSET,
    value=UNSET,
    entity_key=UNSET,
    entity_kind=UNSET,
    metric=UNSET,
    conn=None,
) -> dict:
    """Correct a MANUAL context event in place; return the corrected row.

    Migration 286 / audit 2026-08-17 P1.2: a manual event with the wrong date or
    the wrong label fed `narrative`, `summarizer` and `get_events` forever,
    because this table had no update path at all. Any table the product cites as a
    CAUSE carries a human correction path.

    ONLY THE MANUAL HALF. A connector-sourced row is derived from its Datastream's
    Event Configuration and re-emitted by the next pull, so a hand correction is a
    value the next ingest erases. It is refused with the gesture that actually
    repairs it, never with a technical cause.

    A RETIRED ROW IS NOT CORRECTED EITHER. It is the record of a withdrawal that
    already happened; editing it would rewrite the evidence rather than add to it.
    The refusal names the gesture: write a new event.

    Validation is the SAME validation the create door runs -- `validate_event_input`
    and `validate_event_type` -- applied to the row AS IT WILL BE, not to the
    fields the caller happened to send: correcting a date alone must still be
    judged against the label that stays, or a rule would hold on create and not on
    correction.

    ``conn`` -- as on `persist_context_event`, and for the same stated reason: the
    caller hands in a connection ALREADY carrying its access context
    (`core.db.request_connection`), so the row-level floor is the second barrier
    under the scope check the route just passed. When ``conn`` is given the caller
    OWNS it: this function commits on it and never closes it.

    Raises `ContextEventRefusal` (not found / connector-owned / already retired)
    and `fastmcp.exceptions.ToolError` on invalid input, exactly like the create
    door -- the two doors refuse the same input the same way.
    """
    from contextlib import nullcontext  # noqa: PLC0415

    from core.audit import insert_audit_row  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    given = {
        "event_date": event_date,
        "type": type,
        "label": label,
        "description": description,
        "platform": platform,
        "value": value,
        # `UNSET` and `None` differ here exactly as they do for `platform`:
        # not given leaves the metric alone, an explicit null widens the event
        # back to "about every metric", which is a correction someone needs.
        "metric": normalize_metric(metric) if metric is not UNSET else UNSET,
    }

    with (nullcontext(conn) if conn is not None else get_connection()) as conn:
        current = _locked_manual_event(
            conn, project_id=project_id, event_id=event_id, gesture="correct"
        )

        merged_date = (
            str(current["event_date"]) if event_date is UNSET else str(event_date)
        )
        merged_label = current["label"] if label is UNSET else label
        merged_type = current["type"] if type is UNSET else type
        validate_event_input(merged_label, merged_date)
        validate_event_type(merged_type, "manual")
        # Judged on the row AS IT WILL BE, like the two above: a correction that
        # only renames the metric must meet the same catalogue the create door
        # meets, or a rule would hold on create and not on correction.
        if given["metric"] is not UNSET:
            assert_metric_is_governed(conn, given["metric"], project_id)

        changes: dict = {
            column: given[column]
            for column in _CORRECTABLE_COLUMNS
            if given[column] is not UNSET
        }

        # The entity pair is resolved against what the row already carries: a
        # caller correcting a mistyped key alone must not lose the kind, and the
        # CHECK of migration 262 refuses the half-pair either way.
        if entity_key is not UNSET or entity_kind is not UNSET:
            merged_key = current["entity_key"] if entity_key is UNSET else entity_key
            merged_kind = current["entity_kind"] if entity_kind is UNSET else entity_kind
            changes["entity_key"], changes["entity_kind"] = normalize_entity_pair(
                merged_key, merged_kind, source="manual", gesture="update_manual_event"
            )

        if not changes:
            raise ContextEventRefusal(
                "no_change",
                "Name at least one field to correct.",
                status=422,
            )

        # Column names come from `_CORRECTABLE_COLUMNS` and the entity pair, never
        # from the caller; only the VALUES are parameters.
        assignments = ", ".join(
            f"{column} = %s::date" if column == "event_date" else f"{column} = %s"
            for column in changes
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.context_events
                   SET {assignments}
                 WHERE id = %s AND project_id = %s
             RETURNING {", ".join(_EVENT_ROW_COLS)}
                """,  # noqa: S608 -- assignments is built from a fixed allowlist
                (*changes.values(), event_id, project_id),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover -- the row is locked above
                raise RuntimeError("UPDATE RETURNING returned no row")
            corrected = serialize_event_row([d[0] for d in cur.description], row)

        # Migration 328: the superseded wording becomes a ROW, in the same
        # transaction as the correction that superseded it. Before this, it
        # survived only inside the audit blob below -- a payload no reader could
        # reach, which is why the list route answered `version_history: false`.
        # Written under the `FOR UPDATE` lock `_locked_manual_event` already
        # holds, so `MAX(revision_no) + 1` cannot race a second corrector.
        _insert_event_revision(
            conn,
            event_id=event_id,
            project_id=project_id,
            corrected_by=updated_by,
            corrected_fields=sorted(changes),
            previous=current,
        )

        # The row is OVERWRITTEN by a correction, and the audit row is where the
        # gesture is accounted for -- WHO corrected WHAT, beside every other act
        # of this identity. It keeps the superseded values too: an audit line that
        # names only the fields would say that something changed and never what it
        # was, and the two records answer different questions (an audit trail is
        # read by actor, a revision history by annotation).
        insert_audit_row(
            conn,
            identity=updated_by,
            action=ACTION_CONTEXT_EVENT_UPDATED,
            provider_account="",
            connection_ref="",
            metadata={
                "event_id": event_id,
                "project_id": project_id,
                "fields": sorted(changes),
                "previous": {
                    column: (
                        str(current[column])
                        if column in ("event_date", "value") and current[column] is not None
                        else current[column]
                    )
                    for column in sorted(changes)
                },
            },
        )
        conn.commit()

    return corrected


def _insert_event_revision(
    conn,
    *,
    event_id: str,
    project_id: str,
    corrected_by: str,
    corrected_fields: list[str],
    previous: dict,
) -> None:
    """File the wording this correction supersedes (migration 328).

    ``previous`` is the row as `_locked_manual_event` read it -- BEFORE the
    UPDATE. Called with that lock still held, so the revision number is allocated
    against a history no other transaction can extend meanwhile.

    Appended, never rewritten: the append-only trigger of 328 refuses an UPDATE
    here, so a superseded wording cannot be re-worded by a later hand. Correcting
    the annotation again files the NEXT revision.
    """
    from ulid import ULID  # noqa: PLC0415

    columns = ", ".join(_REVISION_COLUMNS.values())
    placeholders = ", ".join(["%s"] * len(_REVISION_COLUMNS))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.context_event_revisions
                (id, event_id, project_id, revision_no, corrected_by,
                 corrected_fields, {columns})
            SELECT %s, %s, %s,
                   COALESCE(MAX(revision_no), 0) + 1, %s, %s, {placeholders}
              FROM app.context_event_revisions
             WHERE event_id = %s
            """,  # noqa: S608 -- the column list is a module constant
            (
                f"cer_{ULID()}",
                event_id,
                project_id,
                corrected_by,
                corrected_fields,
                *(previous[column] for column in _REVISION_COLUMNS),
                event_id,
            ),
        )


def fetch_event_row(conn, *, project_id: str, event_id: str) -> dict | None:
    """One event of THIS project, or ``None`` -- the read a screen opens with.

    Scoped by project as well as by id, like every other door here: an event of
    another project answers exactly as one that exists nowhere. The column list
    is the one a correction and a retirement hand back, so an event read here and
    an event returned by a write are the same record.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_EVENT_ROW_COLS)}
              FROM app.context_events
             WHERE id = %s AND project_id = %s
            """,  # noqa: S608 -- the column list is a module constant
            (event_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return serialize_event_row([desc[0] for desc in cur.description], row)


def fetch_event_revisions(
    *, project_id: str, event_id: str, conn=None
) -> list[dict]:
    """The wordings this annotation had before its corrections, oldest first.

    Scoped by ``project_id`` as well as by event id, for the reason
    `_locked_manual_event` states: an event of another project must answer
    exactly as one that exists nowhere. An empty list is therefore ambiguous ON
    PURPOSE -- "never corrected" and "not yours to read" are the same answer, and
    the route decides which by asking whether the EVENT is readable first.

    ``conn`` -- same contract as the write doors: given, it is the caller's.
    """
    from contextlib import nullcontext  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    with (nullcontext(conn) if conn is not None else get_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_REVISION_ROW_COLS)}
                  FROM app.context_event_revisions
                 WHERE event_id = %s AND project_id = %s
                 ORDER BY revision_no ASC
                """,  # noqa: S608 -- the column list is a module constant
                (event_id, project_id),
            )
            cols = [desc[0] for desc in cur.description]
            return [serialize_revision_row(cols, row) for row in cur.fetchall()]


def serialize_revision_row(cols, row) -> dict:
    """One revision, in the vocabulary the screen reads.

    `previous_` is dropped from every key: the record already says it is a
    previous wording, and the screen renders it under a heading that says so
    again. Dates, timestamps and NUMERIC are converted exactly as
    `serialize_event_row` converts them, so a revision and an event are read the
    same way by the same component.
    """
    record: dict = {}
    for col, val in zip(cols, row):
        key = col[len("previous_"):] if col.startswith("previous_") else col
        if val is None:
            record[key] = None
        elif col == "superseded_at":
            record[key] = val.isoformat()
        elif col in ("previous_event_date",):
            record[key] = str(val)
        elif col == "previous_value":
            record[key] = float(val)
        elif col == "corrected_fields":
            record[key] = list(val)
        else:
            record[key] = val
    return record


def retire_manual_event(
    *,
    project_id: str,
    event_id: str,
    retired_by: str,
    reason: str,
    conn=None,
) -> dict:
    """Retire a MANUAL context event -- a supersede, never a delete.

    The row stays: it was a candidate cause in an anomaly alert, a marker on a
    card, a regressor in a feature matrix. Destroying it would make those earlier
    answers unexplainable, and would lose the difference between "this was never
    said" and "this was said, and withdrawn". It stops being SERVED instead --
    every read below filters `retired_at IS NULL`.

    The reason is REQUIRED and is not decoration: without it, the next reader
    cannot tell a wrong event from an inconvenient one, and the retirement itself
    becomes the thing nobody can judge. Migration 286 CHECKs the same rule.

    A retirement is never unmade -- the trigger of 286 refuses it, so a caller who
    retired the wrong event writes the event again rather than resurrecting this
    one. A connector-sourced row is refused with the gesture that stops it at the
    source.

    ``conn`` -- same contract as `persist_context_event` and `update_manual_event`.
    """
    from contextlib import nullcontext  # noqa: PLC0415

    from core.audit import insert_audit_row  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    reason_clean = str(reason).strip() if reason else ""
    if not reason_clean:
        raise ContextEventRefusal(
            "missing_reason",
            "Say why this event should stop being read -- a retirement with no "
            "reason is one nobody can judge later.",
            status=422,
        )

    with (nullcontext(conn) if conn is not None else get_connection()) as conn:
        _locked_manual_event(
            conn, project_id=project_id, event_id=event_id, gesture="retire"
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.context_events
                   SET retired_at = now(), retired_by = %s, retired_reason = %s
                 WHERE id = %s AND project_id = %s
             RETURNING {", ".join(_EVENT_ROW_COLS)}
                """,  # noqa: S608 -- the column list is a module constant
                (retired_by, reason_clean, event_id, project_id),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover -- the row is locked above
                raise RuntimeError("UPDATE RETURNING returned no row")
            retired = serialize_event_row([d[0] for d in cur.description], row)

        insert_audit_row(
            conn,
            identity=retired_by,
            action=ACTION_CONTEXT_EVENT_RETIRED,
            provider_account="",
            connection_ref="",
            metadata={
                "event_id": event_id,
                "project_id": project_id,
                "reason": reason_clean,
            },
        )
        conn.commit()

    return retired


def delete_connector_events_in_window(
    *,
    project_id: str,
    source: str,
    event_type: str,
    date_from: str,
    date_to: str,
    conn=None,
) -> int:
    """Delete connector-emitted context events in a window; return rows deleted (Epic 31.3).

    Idempotence primitive for connector event pulls (H1). A connector that
    re-pulls the same window daily would otherwise INSERT duplicate markers on
    every run (persist_context_event inserts unconditionally), doubling the MMM
    regressor and breaking the honesty invariant. The fix is a delete-by-source-
    window BEFORE the re-insert: a re-pull of the same (project_id, source,
    event_type, [date_from, date_to]) becomes idempotent WITHOUT a new migration
    and WITHOUT a DB-level unique constraint.

    Scoped defensively (AD-8 project-scoped, additive):
      * project_id   -- never touches another project's events.
      * source       -- never deletes MANUAL events (source='manual') nor another
                        connector's events; only rows this connector wrote.
      * event_type   -- only the type this profile re-emits (a mixed connector
                        with several event profiles clears each independently).
      * [date_from, date_to] -- only the re-pulled window.

    No-op (returns 0) when the DB is unreachable is NOT swallowed here: a delete
    failure must surface (unlike a best-effort read) so a caller never re-inserts
    on top of stale rows. Raises fastmcp.exceptions.ToolError on a DB error.
    ``conn`` -- Story 68.4. An IMPORT owns its transaction: the delete-window
    and the re-insert that follows it must land or roll back together, or a
    refused import would have already erased the window it never replaced.
    When a connection is given the caller OWNS it -- this function neither
    commits nor closes it. The connector pulls keep the unarmed acquisition.
    """
    from contextlib import nullcontext  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    caller_owns = conn is not None
    try:
        with (nullcontext(conn) if caller_owns else get_connection()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM app.context_events
                    WHERE project_id = %s
                      AND source = %s
                      AND type = %s
                      AND event_date BETWEEN %s::date AND %s::date
                    """,
                    (project_id, source, event_type, date_from, date_to),
                )
                deleted = cur.rowcount if cur.rowcount is not None else 0
            if not caller_owns:
                conn.commit()
    except Exception as exc:
        logger.error("delete_connector_events_in_window: db_error: %s", exc)
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database error: {exc}"})
        ) from exc

    logger.info(
        "delete_connector_events_in_window: deleted=%d project_id=%s source=%s "
        "type=%s window=[%s,%s]",
        deleted, project_id, source, event_type, date_from, date_to,
    )
    return deleted


# ISO-8601 date pattern (YYYY-MM-DD) -- shared with main._ISO_DATE_RE semantics.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


#: The landings a profile may declare, READ FROM the published manifest schema
#: rather than repeated here (Story 64.12, AI-232).
#:
#: WHAT THIS REPLACES AND WHY. `resolve_landing` used to carry its own literal
#: tuple `("fact_daily_kpi", "context_events")`. The manifest schema carries the
#: same fact as an `enum`, and the loader enforces it -- so a MANIFEST could never
#: smuggle an unknown landing past it. The defect was the other direction: two
#: lists holding one meaning drift the moment somebody extends one. Add a landing
#: to the schema enum and this function would have silently DEGRADED it to
#: `fact_daily_kpi` -- routing a whole new destination into the fact table with no
#: error anywhere, because the queue's caller wraps this in a `try/except
#: Exception -> logger.warning` and would have swallowed a raise too.
#:
#: Reading the schema removes the second copy instead of trying to keep the two in
#: step. Cached at first use; the file is already read by `core.loader` at import.
_LANDINGS: frozenset[str] | None = None


def known_landings() -> frozenset[str]:
    """The declared landing vocabulary, from the manifest schema. Cached."""

    global _LANDINGS
    if _LANDINGS is None:
        import json  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        schema = json.loads(
            (Path(__file__).parent / "schemas" / "manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        values = (
            schema.get("properties", {})
            .get("report_profiles", {})
            .get("items", {})
            .get("properties", {})
            .get("landing", {})
            .get("enum")
        )
        if not values:
            # A schema that stopped declaring the vocabulary is a repository
            # defect, not a reason to invent one here. Refusing loudly beats
            # falling back to a literal, which is the copy this change removes.
            raise RuntimeError(
                "manifest.schema.json declares no landing enum -- the routing "
                "vocabulary has no owner"
            )
        _LANDINGS = frozenset(values)
    return _LANDINGS


def resolve_landing(profile: dict, module_kind: str | None) -> str:
    """Return where a profile lands (Epic 31; vocabulary from the schema, 64.12).

    Per-profile 'landing' wins; absent, it is DERIVED from module_kind
    (module_kind=='context' -> context_events, else fact_daily_kpi). This is the
    single routing decision -- module_kind no longer decides on its own, so a
    connector may mix kpi and event profiles.

    An ABSENT landing derives, and that is legitimate: most manifests declare
    none. A DECLARED landing is honoured only if the schema knows it; anything
    else would be a value the loader already refuses, so reaching here means the
    schema and this function disagree -- and the caller in `core.queue` swallows
    exceptions into a warning, so silence is what a raise would buy. Deriving the
    vocabulary is what makes the disagreement impossible instead of merely loud.
    """
    landing = (profile or {}).get("landing")
    if landing in known_landings():
        return landing
    return "context_events" if module_kind == "context" else "fact_daily_kpi"


def validate_event_type(event_type: str, source: str) -> None:
    """Enforce the canonical event vocabulary for CONNECTOR events (Epic 31).

    A connector-emitted event (source != 'manual') MUST carry an event_type known
    to dim_event_type.csv -- an unknown type is a drift signal (typed refusal),
    NEVER a silent drop, exactly like an unknown metric. Manual events
    (source == 'manual') keep free-form types (backward compatibility with the
    console; only a debug log on an unknown type).

    Raises fastmcp.exceptions.ToolError on connector drift.
    """
    from core.report_dictionary import is_known_event_type  # noqa: PLC0415

    if is_known_event_type(event_type):
        return
    if source == "manual":
        logger.debug("context_event: manual event with non-canonical type %r", event_type)
        return
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    raise ToolError(
        json.dumps(
            {
                "code": "unknown_event_type",
                "message": (
                    f"event_type inconnu du dictionnaire canonique (dim_event_type.csv) : "
                    f"{event_type!r} (source={source!r}) -- drift, jamais un drop silencieux."
                ),
            }
        )
    )


def validate_event_input(label: str, event_date: str) -> None:
    """Validate shared inputs for context events (MCP tool + REST endpoint).

    Raises:
        fastmcp.exceptions.ToolError: with French error message on failure.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    if len(label) > 120:
        raise ToolError(
            json.dumps(
                {"code": "invalid_input", "message": "label is too long (max 120 characters)"}
            )
        )
    if not _ISO_DATE_RE.match(event_date):
        raise ToolError(
            json.dumps(
                {
                    "code": "invalid_input",
                    "message": f"event_date invalide (format attendu YYYY-MM-DD) : {event_date!r}",
                }
            )
        )


# Columns exposed by the analytical read path, in select order.
# Epic 31 (migration 055) adds platform/value/source so the overlay (annotation
# markers) AND the MMM feature-builder see the full event identity
# (platform>type>label) + the optional regressor magnitude (value) + provenance
# (source). These are additive/nullable: legacy rows carry platform/value NULL and
# source='manual', and a pre-055 DuckDB mirror simply lacks the columns (handled
# below by intersecting with the table's actual columns -- backward compatible).
_BASE_EVENT_COLS = ("id", "project_id", "event_date", "type", "label")
_MMM_EVENT_COLS = ("platform", "value", "source")

#: Migration 322 -- the metric an event names, or NULL for "every metric". Read
#: defensively like the MMM trio above, and for the same reason: a mirror synced
#: before 322 simply lacks the column, and a bare SELECT would turn the whole
#: read into a BinderException. Absent here, the scoped walks fall back to the
#: narrower comparison and SAY that the dimension could not be checked.
_SCOPE_EVENT_COLS = ("metric",)

#: The lifecycle columns of migration 286. Served ONLY to a caller that asked for
#: retired rows: on the default read they would carry NULL for every row and mean
#: nothing, and a column that is always NULL is how a reader learns to ignore it.
_LIFECYCLE_EVENT_COLS = ("retired_at", "retired_by", "retired_reason")


def mirror_live_events_clause(con) -> str:
    """``AND retired_at IS NULL``, or ``""`` on a mirror that predates 286.

    THE CLASS, not the instance. `mirror.context_events` is read by
    `fetch_context_events` (which feeds cards, reports, briefings and
    `get_events`) AND by `anomaly_alerts._fetch_context_events_for_anomaly`, which
    lists candidate CAUSES. A retirement honoured on one of the two would be a
    retirement the product still cites -- exactly the defect this whole change
    repairs, moved one read to the left.

    Guarded on the column rather than on a `try/except` around the query: the
    caller's fallback for a missing column is a query with NO retirement filter,
    so an exception-driven guard would silently serve retired events on precisely
    the mirrors that cannot filter them. Here the absence is decided BEFORE the
    read and can be logged. One catalogue lookup per call, on a local DuckDB file.
    """
    try:
        rows = con.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'mirror' AND table_name = 'context_events' "
            "AND column_name = 'retired_at'"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 -- an absent mirror is the caller's problem
        logger.debug("context_events: retirement_column_probe_failed: %s", exc)
        return ""
    if rows:
        return " AND retired_at IS NULL"
    logger.debug(
        "context_events: mirror predates migration 286 -- retired events cannot "
        "be filtered out of this read yet (re-run mirror_sync)"
    )
    return ""


def mirror_has_metric_column(con) -> bool:
    """Whether this mirror carries migration 322's ``metric`` column.

    THE CLASS, not the instance -- the same guard `mirror_live_events_clause`
    states above, for the same reason. `anomaly_alerts` filters candidate causes
    INSIDE its SQL, so an absent column has to be decided BEFORE the read: an
    exception-driven guard would fall back to a query with no metric
    discriminant at all, on precisely the mirrors that cannot apply one, and the
    descriptor would still claim the dimension was checked.

    A `False` is not a defect to hide: it is the honest pre-322 state, and the
    caller reports `metric` as an unscoped dimension exactly as it did before.
    """
    try:
        rows = con.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'mirror' AND table_name = 'context_events' "
            "AND column_name = 'metric'"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 -- an absent mirror is the caller's problem
        logger.debug("context_events: metric_column_probe_failed: %s", exc)
        return False
    if rows:
        return True
    logger.debug(
        "context_events: mirror predates migration 322 -- a claim's metric "
        "cannot be compared to an event's yet (re-run mirror_sync)"
    )
    return False


def mirror_has_context_events_table(con) -> bool:
    """Whether this warehouse connection carries ``mirror.context_events`` at all.

    THE CLASS, not the instance -- the third probe beside the two above, for the
    reader that names a candidate CAUSE out loud (`anomaly_alerts`). On a
    deployment that keeps no mirror the table does not exist, and until AI-344
    that reader caught the CatalogException and answered "no cause" -- the same
    silent empty list `fetch_context_events` served. Decided BEFORE the read so
    the caller can turn to the record instead of to an empty answer.
    """
    try:
        rows = con.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'mirror' AND table_name = 'context_events'"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 -- an unreadable mirror is "not a store"
        logger.debug("context_events: mirror_table_probe_failed: %s", exc)
        return False
    return bool(rows)


def fetch_context_events(
    project_id: str,
    start: str,
    end: str,
    *,
    include_retired: bool = False,
    identity: str | None = None,
    conn=None,
) -> list[dict]:
    """The live context events of *project_id* between *start* and *end*, inclusive.

    SERVED IN THIS ORDER (context-hub.md, amendment of 2026-09-01, AI-344):

    1. the DuckDB mirror ``mirror.context_events`` when ``TOOROW_DUCKDB_PATH``
       names an existing file that carries the table -- the ``duckdb`` deployment
       shape. Column allowlists, retired semantics and output shape are unchanged;
    2. otherwise the system of record, Postgres ``app.context_events`` -- the
       ``bigquery`` deployment shape, where ``mirror_sync`` defers its writes and
       there is no mirror to read. Same columns, same retired semantics, same
       shape. The read rides *conn* when the caller holds one (a background path
       with no human), else the caller's SCOPED connection opened for *identity*
       (``core.db.request_connection``). It never writes;
    3. when neither can serve, ``ContextEventsUnavailable`` -- never ``[]``.

    An empty list therefore means exactly one thing: a store was read, and this
    project has zero live events in this window. Measured 2026-09-01: the mirror
    was the only path, the deployment kept none, and ``get_events`` answered
    ``count: 0`` for a project holding six events.

    Epic 31.2: exposes ``platform``, ``value`` and ``source`` (migration 055) so
    the same read feeds BOTH the annotation overlay AND the MMM feature-builder.
    On the mirror the columns are selected defensively -- only those actually
    present -- so a pre-055 mirror still returns the base columns.

    Migration 286: a RETIRED event is not served here. It is not dropped from a
    list that claims to be complete either -- ``include_retired=True`` returns it,
    carrying its ``retired_at`` / ``retired_by`` / ``retired_reason`` so the caller
    reads a withdrawal rather than a row. The default is the honest one because
    every caller of this function (cards, reports, briefings, ``get_events``,
    the scheduler) asks it what a project OBSERVES, not what it once recorded.

    Returns rows WHERE project_id = ? AND event_date BETWEEN ? AND ? ordered by
    event_date ASC; ``event_date`` (and ``retired_at``) as strings, ``value`` as
    a float or ``None`` (unit pulse, AD-9).

    AD-2: no module-specific strings -- purely generic project-scoped parameters.
    """
    db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    if db_path and os.path.exists(db_path):
        served = _fetch_context_events_from_mirror(
            db_path, project_id, start, end, include_retired=include_retired
        )
        if served is not None:
            return served
        logger.debug(
            "context_events: mirror at %r cannot serve -- reading the record", db_path
        )

    if conn is not None:
        try:
            return _fetch_context_events_from_postgres(
                conn, project_id, start, end, include_retired=include_retired
            )
        except Exception as exc:
            logger.warning(
                "context_events_record_fetch_failed: project_id=%s: %s: %s",
                project_id, type(exc).__name__, exc,
            )
            raise ContextEventsUnavailable(
                _RECORD_UNREACHABLE_REASON, _RECORD_UNREACHABLE_REPAIR
            ) from exc

    if not identity:
        raise ContextEventsUnavailable(
            "The context events were not read: this read was asked for without a "
            "caller, so no scoped connection to the events store could be opened.",
            "Call this read from an authenticated session, or hand it the "
            "connection the background job already holds.",
        )

    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as scoped:
            return _fetch_context_events_from_postgres(
                scoped, project_id, start, end, include_retired=include_retired
            )
    except ContextEventsUnavailable:
        raise
    except Exception as exc:
        logger.warning(
            "context_events_record_fetch_failed: project_id=%s: %s: %s",
            project_id, type(exc).__name__, exc,
        )
        raise ContextEventsUnavailable(
            _RECORD_UNREACHABLE_REASON, _RECORD_UNREACHABLE_REPAIR
        ) from exc


_RECORD_UNREACHABLE_REASON = (
    "The context events were not read: this deployment keeps no events mirror and "
    "the events store could not be reached."
)
_RECORD_UNREACHABLE_REPAIR = (
    "Retry in a moment. If it persists, repair the database connection of this "
    "deployment (PLATFORM_DB_URL), or point TOOROW_DUCKDB_PATH at a synced mirror."
)


def _event_record(cols: list[str], row: tuple) -> dict:
    """One output row, in the ONE shape both stores share.

    ``event_date`` and ``retired_at`` land as objects out of either store and
    travel to a JSON channel; ``str`` is what this path has always done. NUMERIC
    ``value`` becomes a float; NULL stays ``None`` (unit pulse, AD-9).
    """
    record: dict = {}
    for col, val in zip(cols, row):
        if col in ("event_date", "retired_at") and val is not None:
            record[col] = str(val)
        elif col == "value" and val is not None:
            record[col] = float(val)
        else:
            record[col] = val
    return record


def _fetch_context_events_from_mirror(
    db_path: str, project_id: str, start: str, end: str, *, include_retired: bool
) -> list[dict] | None:
    """The mirror's answer, or ``None`` when this mirror cannot serve at all.

    ``None`` -- the table was never synced into this file, or the file could not
    be read -- is NOT an empty window: the caller falls through to the record. An
    empty list IS one: the table exists and holds nothing for this project here.
    """
    import duckdb  # noqa: PLC0415

    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            # Resolve which MMM columns actually exist in the mirror table so a
            # pre-055 mirror does not raise a BinderException on an absent column.
            available = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'mirror' AND table_name = 'context_events'"
                ).fetchall()
            }
            if not available:
                # No table: the mirror was never synced. Distinct from an empty
                # table (which `mirror_sync` writes on purpose, with its columns).
                logger.warning(
                    "mirror.context_events not yet synced at %r -- reading the "
                    "record instead (run mirror_sync.sync_tables() or set "
                    "SYNC_ENABLED=true)",
                    db_path,
                )
                return None
            select_cols = list(_BASE_EVENT_COLS) + [
                c for c in (*_MMM_EVENT_COLS, *_SCOPE_EVENT_COLS) if c in available
            ]
            if include_retired:
                select_cols += [c for c in _LIFECYCLE_EVENT_COLS if c in available]
            col_sql = ", ".join(select_cols)
            live_only = "" if include_retired else mirror_live_events_clause(con)
            rel = con.execute(
                f"""
                SELECT {col_sql}
                FROM mirror.context_events
                WHERE project_id = ?
                  AND event_date BETWEEN ? AND ?
                  {live_only}
                ORDER BY event_date ASC
                """,  # noqa: S608 -- col names are from a fixed allowlist, not user input
                [project_id, start, end],
            )
            cols = [d[0] for d in rel.description]
            return [_event_record(cols, row) for row in rel.fetchall()]
        finally:
            con.close()
    except Exception as exc:
        logger.warning("context_events_mirror_fetch_failed: project_id=%s: %s", project_id, exc)
        return None


def _fetch_context_events_from_postgres(
    conn, project_id: str, start: str, end: str, *, include_retired: bool
) -> list[dict]:
    """The record's answer -- ``app.context_events`` -- in the mirror's shape.

    The columns are the full allowlist rather than a probe: the record is the
    migrated schema (055, 286, 322 all applied before any row could carry them),
    where a mirror can lag its own sync. A read, on the connection it was handed;
    it commits nothing and rolls nothing back.
    """
    select_cols = list(_BASE_EVENT_COLS) + list(_MMM_EVENT_COLS) + list(_SCOPE_EVENT_COLS)
    if include_retired:
        select_cols += list(_LIFECYCLE_EVENT_COLS)
    col_sql = ", ".join(select_cols)
    live_only = "" if include_retired else " AND retired_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {col_sql}
            FROM app.context_events
            WHERE project_id = %s
              AND event_date BETWEEN %s::date AND %s::date
              {live_only}
            ORDER BY event_date ASC
            """,  # noqa: S608 -- col names are from a fixed allowlist, not user input
            (project_id, start, end),
        )
        cols = [d[0] for d in cur.description]
        return [_event_record(cols, row) for row in cur.fetchall()]
