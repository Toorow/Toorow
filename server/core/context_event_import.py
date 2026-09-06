"""toorow -- the EVENTS route of a managed-feed import (Story 68.4).

Epic 68 routes ONE import engine three ways, by declaration and never by
guessing from the rows:

  * facts     -- the default: typed rows to the project-scoped raw landing;
  * events    -- a file whose pinned mapping declares EVENT ROLES lands as
                 datastream-owned context events, so the LLM can cite them in
                 the why-layer of a report;
  * reference -- versioned MDM attributes (Story 68.5).

WHAT MAKES A MAPPING AN EVENTS MAPPING. Roles, declared on the bindings of the
pinned mapping (``fields[].binding.event_role``), all read from the governed,
immutable, pinnable store -- never a second one:

  * ``date`` and ``label`` are REQUIRED: a marker with no day marks nothing,
    and a marker with no words says nothing;
  * ``type`` is REQUIRED and is a COLUMN, not a constant hidden in code: the
    file states what kind of thing each of its rows is, and the canonical
    vocabulary (``dim_event_type.csv``) is what refuses a drift;
  * ``description``, ``value`` and ``entity`` are optional.

A role appears AT MOST ONCE. Two columns claiming the same role is two answers
to one question, and picking between them here is exactly the silent guess
this epic exists to remove.

THE WRITERS ARE THE ONLY WRITERS. ``persist_context_event`` and
``delete_connector_events_in_window`` -- both now able to run on the import's
own transaction (Story 68.4), because a landing whose events committed before
its ledger row would leave markers behind a refused import. Nothing here
issues an INSERT of its own.

IDEMPOTENCE IS A DELETE-WINDOW, PER TYPE. A re-imported calendar must not
double its markers, and ``persist_context_event`` inserts unconditionally.
Before the first insert the route deletes ``(project, source, type, [min..max
date of the file])`` for each type the file carries -- the connector-pull
precedent, scoped by the file's OWN window so it never touches a day the file
does not talk about.

AD-2: no event type, no provider and no entity kind is named in this module.
The vocabulary lives in ``dim_event_type.csv`` and the kinds are opaque
strings.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping

from core.file_source_resolution import (
    _CONFIRMED_BINDING_STATES,
)
from core.tabular_parsing import (
    _truncate_value,
)
from core.tabular_types import (
    CsvExcelImportError,
    RejectedRow,
)

logger = logging.getLogger(__name__)


#: The route name the import runner's ONE router returns for this destination.
ROUTE_CONTEXT_EVENTS = "context_events"

#: The ledger's ``landing_relation`` prefix for an events landing. Like the
#: reference route's, it NAMES what was written rather than a `schema.table`,
#: and `assert_managed_landing` whitelists the prefix so a caller can never
#: point the raw writer at it.
LANDING_RELATION_PREFIX = "context_events:"

#: What a column may CONTRIBUTE to an event. Closed, and mirrored by the
#: `event_role` enum of `datastream-field-mapping.schema.json` -- two lists
#: holding one meaning drift the moment somebody extends one, so the test
#: `test_the_schema_enum_and_the_module_agree` compares them.
ROLE_DATE = "date"
ROLE_TYPE = "type"
ROLE_LABEL = "label"
ROLE_DESCRIPTION = "description"
ROLE_VALUE = "value"
ROLE_ENTITY = "entity"
EVENT_ROLES = (ROLE_DATE, ROLE_TYPE, ROLE_LABEL, ROLE_DESCRIPTION, ROLE_VALUE, ROLE_ENTITY)
REQUIRED_ROLES = (ROLE_DATE, ROLE_TYPE, ROLE_LABEL)

#: Declaration refusals (closed). Each names what is missing, so the repair is
#: a gesture on the mapping and not a debugging session.
REASON_UNKNOWN_ROLE = "event_role_unknown"
REASON_DUPLICATE_ROLE = "event_role_declared_twice"
REASON_MISSING_ROLE = "event_role_missing"
REASON_ROLE_NOT_LANDED = "event_role_column_not_landed"

#: Per-row rejection rules (closed). Evidence in `managed_feed_rejected_rows`
#: carries one of these, the field and the file line -- never a silent drop.
RULE_EVENT_VALIDATION_FAILED = "event_validation_failed"

#: The source every imported event carries. A FAMILY name, not a provider and
#: not a Datastream id: `delete_connector_events_in_window` scopes on it, and
#: the Datastream is already carried by the binding columns.
EVENT_SOURCE = "managed_feed"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LABEL_MAX = 120


class EventDeclarationIssue:
    """One reason a mapping's event declaration cannot be honoured."""

    __slots__ = ("field_id", "reason", "message")

    def __init__(self, field_id: str | None, reason: str, message: str) -> None:
        self.field_id = field_id
        self.reason = reason
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EventDeclarationIssue({self.field_id!r}, {self.reason!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, EventDeclarationIssue)
            and (self.field_id, self.reason) == (other.field_id, other.reason)
        )


# ---------------------------------------------------------------------------
# The declaration. Pure, so the routing rule is provable without a database.
# ---------------------------------------------------------------------------


def event_roles(mapping_payload: Mapping[str, Any] | None) -> list[tuple[str, str, str]]:
    """``(field_id, role, canonical_target)`` for every LANDED role declared.

    "Landed" mirrors ``_apply_governed_mapping`` exactly (a confirmed binding
    with a named canonical target): a column the mapping would not land cannot
    contribute to an event either, and reading it here would promise a value
    that never arrives.
    """
    out: list[tuple[str, str, str]] = []
    for item in (mapping_payload or {}).get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        binding = item.get("binding") or {}
        role = binding.get("event_role")
        if role is None or not str(role).strip():
            continue
        field_id = str(item.get("field_id") or "").strip()
        target = str(binding.get("canonical_target") or "").strip()
        status = str(binding.get("status") or "").lower()
        if status in _CONFIRMED_BINDING_STATES and field_id and target:
            out.append((field_id, str(role).strip(), target))
        else:
            out.append((field_id, str(role).strip(), ""))
    return out


def validate_event_declaration(
    mapping_payload: Mapping[str, Any] | None,
) -> list[EventDeclarationIssue]:
    """Every reason this mapping's event declaration is not executable. Pure.

    Returns an empty list when the mapping declares NO role at all: a facts
    mapping is not a broken events mapping, and refusing it would make every
    existing Datastream non-executable.
    """
    declared = event_roles(mapping_payload)
    if not declared:
        return []

    issues: list[EventDeclarationIssue] = []
    seen: dict[str, str] = {}
    for field_id, role, target in declared:
        if role not in EVENT_ROLES:
            issues.append(
                EventDeclarationIssue(
                    field_id,
                    REASON_UNKNOWN_ROLE,
                    f"column {field_id!r} declares the unknown event role {role!r}; "
                    f"the roles are {', '.join(EVENT_ROLES)}",
                )
            )
            continue
        if not target:
            issues.append(
                EventDeclarationIssue(
                    field_id,
                    REASON_ROLE_NOT_LANDED,
                    f"column {field_id!r} carries the event role {role!r} but the "
                    "mapping does not land it; confirm the column or drop the role",
                )
            )
            continue
        if role in seen:
            issues.append(
                EventDeclarationIssue(
                    field_id,
                    REASON_DUPLICATE_ROLE,
                    f"the event role {role!r} is declared by {seen[role]!r} and by "
                    f"{field_id!r}; one role names one column",
                )
            )
            continue
        seen[role] = field_id

    for role in REQUIRED_ROLES:
        if role not in seen:
            issues.append(
                EventDeclarationIssue(
                    None,
                    REASON_MISSING_ROLE,
                    f"this mapping declares event roles but no column carries {role!r}; "
                    f"an event needs {', '.join(REQUIRED_ROLES)}",
                )
            )
    return issues


def event_declaration(
    mapping_payload: Mapping[str, Any] | None,
    projection_plan: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The events-route declaration of a pinned mapping, or None.

    Returns ``{role: canonical_target}`` under ``targets``, plus the entity
    kind the entity column designates when it also carries 68.2's designation
    -- the two declarations compose (an event ABOUT a governed entity), they
    never fork a second identity model.
    """
    declared = event_roles(mapping_payload)
    if not declared:
        return None
    if validate_event_declaration(mapping_payload):
        # A broken declaration is refused at append and at import; routing it
        # here would land half a calendar.
        return None
    if (projection_plan or {}).get("additive_measures"):
        # A file with measures carries facts, and facts take the warehouse
        # route. An events file's `value` role is one optional magnitude per
        # marker, never a measure the mart sums.
        return None

    targets = {role: target for _field_id, role, target in declared}
    fields = {role: field_id for field_id, role, _target in declared}
    entity_kind = None
    entity_field = fields.get(ROLE_ENTITY)
    if entity_field:
        for item in (mapping_payload or {}).get("fields") or []:
            if isinstance(item, Mapping) and str(item.get("field_id") or "") == entity_field:
                kind = (item.get("binding") or {}).get("designates_object_kind")
                entity_kind = str(kind).strip() if kind else None
    return {"targets": targets, "fields": fields, "entity_kind": entity_kind}


def resolve_events_route(
    *,
    mapping_payload: Mapping[str, Any] | None,
    projection_plan: Mapping[str, Any] | None,
    publish_candidate: bool,
) -> dict[str, Any] | None:
    """The events route, or None -- refusing what it cannot honour.

    Unlike the reference route this needs no registry read: the event
    vocabulary is a repo catalogue (`dim_event_type.csv`), not a project-scoped
    table, so nothing about the declaration can drift between the pin and the
    import except the mapping itself, which is immutable.
    """
    route = event_declaration(mapping_payload, projection_plan)
    if route is None:
        return None
    if publish_candidate:
        raise CsvExcelImportError(
            "context_events_publication_is_the_import",
            "an events import writes its markers as it lands; the governed "
            "warehouse dispatch has no candidate to promote",
            repair={"run_without_governed_dispatch": True},
        )
    return route


# ---------------------------------------------------------------------------
# Per-row validation. A row that cannot be an event is rejected WITH evidence,
# inside the mapping's own enumeration, so the line numbers agree.
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def event_row_issue(route: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(field, reason)`` when this row cannot be an event, else None. Pure."""
    from core.report_dictionary import is_known_event_type  # noqa: PLC0415

    targets = route["targets"]
    date_value = _text(row.get(targets[ROLE_DATE]))
    if not _ISO_DATE_RE.match(date_value):
        return (
            targets[ROLE_DATE],
            f"the row names no usable day ({date_value!r}); an event marks a "
            "date, written YYYY-MM-DD",
        )
    label = _text(row.get(targets[ROLE_LABEL]))
    if not label:
        return (
            targets[ROLE_LABEL],
            "the row carries no label; a marker with no words says nothing",
        )
    if len(label) > _LABEL_MAX:
        return (
            targets[ROLE_LABEL],
            f"the label exceeds {_LABEL_MAX} characters, which the event store refuses",
        )
    event_type = _text(row.get(targets[ROLE_TYPE])).lower()
    if not is_known_event_type(event_type):
        return (
            targets[ROLE_TYPE],
            f"{event_type!r} is not a declared event type; an unknown type is a "
            "drift signal, never a silently kept marker",
        )
    value_target = targets.get(ROLE_VALUE)
    if value_target is not None and _text(row.get(value_target)):
        try:
            float(_text(row.get(value_target)))
        except (TypeError, ValueError):
            return (
                value_target,
                "the magnitude is not a number; an event's value feeds a model "
                "and a word cannot",
            )
    return None


def event_row_validator(route: Mapping[str, Any]) -> Callable[[dict, int], RejectedRow | None]:
    """The per-row validator for one resolved route, as a mapping row hook."""

    def validate(row: dict[str, Any], row_number: int) -> RejectedRow | None:
        issue = event_row_issue(route, row)
        if issue is None:
            return None
        field_name, reason = issue
        return RejectedRow(
            row_number=row_number,
            field_name=field_name,
            rule=RULE_EVENT_VALIDATION_FAILED,
            reason=reason,
            rejected_value=_truncate_value(row.get(field_name)),
        )

    return validate


# ---------------------------------------------------------------------------
# The landing. Validated rows become context events, through the only writers
# the event store has.
# ---------------------------------------------------------------------------


def event_window(route: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> tuple[str, str] | None:
    """The ``[min, max]`` day the FILE talks about, or None when it is empty.

    The delete-window is the file's own span on purpose: erasing a wider one
    would delete markers of days this import says nothing about.
    """
    dates = sorted(_text(row.get(route["targets"][ROLE_DATE])) for row in rows)
    dates = [value for value in dates if _ISO_DATE_RE.match(value)]
    if not dates:
        return None
    return dates[0], dates[-1]


def land_context_event_rows(
    conn,
    *,
    route: Mapping[str, Any],
    rows: list[dict[str, Any]],
    project_id: str,
    datastream_id: str,
    org_id: str,
    execution_id: str,
    mapping_version_id: str,
    actor: str,
) -> dict[str, Any]:
    """Land validated event rows as datastream-owned context events.

    Three steps, all on the CALLER's transaction:

      1. the Datastream's Event Configuration is armed if it is not already --
         a managed feed has no Connector to derive one from, so it anchors on
         its pinned mapping version (`event_stream_arming`). Without an active
         version the trigger `require_event_observation_binding` refuses every
         row, which is the honest refusal and a useless one here: the stream
         IS declared, by the mapping;
      2. the file's own window is cleared, per type, so a re-import replaces
         rather than doubles;
      3. every row becomes one event, carrying the execution that landed it.
    """
    from core.context_events import (  # noqa: PLC0415
        collecting_for_datastream,
        delete_connector_events_in_window,
        persist_context_event,
    )
    from core.event_stream_arming import ensure_managed_feed_event_stream  # noqa: PLC0415

    targets = route["targets"]
    types = sorted({_text(row.get(targets[ROLE_TYPE])).lower() for row in rows} - {""})

    version_id = ensure_managed_feed_event_stream(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        org_id=org_id,
        actor=actor,
        event_types=types,
        mapping_version_id=mapping_version_id,
    )

    window = event_window(route, rows)
    deleted = 0
    if window is not None:
        for event_type in types:
            deleted += delete_connector_events_in_window(
                project_id=project_id,
                source=EVENT_SOURCE,
                event_type=event_type,
                date_from=window[0],
                date_to=window[1],
                conn=conn,
            )

    written = 0
    with collecting_for_datastream(datastream_id):
        for row in rows:
            value_target = targets.get(ROLE_VALUE)
            raw_value = _text(row.get(value_target)) if value_target else ""
            entity_target = targets.get(ROLE_ENTITY)
            entity_key = _text(row.get(entity_target)) if entity_target else ""
            persist_context_event(
                project_id=project_id,
                event_date=_text(row.get(targets[ROLE_DATE])),
                type=_text(row.get(targets[ROLE_TYPE])).lower(),
                label=_text(row.get(targets[ROLE_LABEL])),
                description=_text(row.get(targets.get(ROLE_DESCRIPTION)))
                if targets.get(ROLE_DESCRIPTION)
                else "",
                created_by=actor,
                value=float(raw_value) if raw_value else None,
                source=EVENT_SOURCE,
                entity_key=entity_key or None,
                entity_kind=route.get("entity_kind") if entity_key else None,
                conn=conn,
                execution_id=execution_id,
                commit=False,
            )
            written += 1

    return {
        "rows": len(rows),
        "events_written": written,
        "events_replaced": deleted,
        "window": list(window) if window else None,
        "event_types": types,
        "event_configuration_version_id": version_id,
        "table": f"{LANDING_RELATION_PREFIX}{datastream_id}",
        "backend": ROUTE_CONTEXT_EVENTS,
        "execution_id": execution_id,
    }


__all__ = [
    "EVENT_ROLES",
    "EVENT_SOURCE",
    "LANDING_RELATION_PREFIX",
    "REASON_DUPLICATE_ROLE",
    "REASON_MISSING_ROLE",
    "REASON_ROLE_NOT_LANDED",
    "REASON_UNKNOWN_ROLE",
    "REQUIRED_ROLES",
    "ROLE_DATE",
    "ROLE_DESCRIPTION",
    "ROLE_ENTITY",
    "ROLE_LABEL",
    "ROLE_TYPE",
    "ROLE_VALUE",
    "ROUTE_CONTEXT_EVENTS",
    "RULE_EVENT_VALIDATION_FAILED",
    "EventDeclarationIssue",
    "event_declaration",
    "event_roles",
    "event_row_issue",
    "event_row_validator",
    "event_window",
    "land_context_event_rows",
    "resolve_events_route",
    "validate_event_declaration",
]
