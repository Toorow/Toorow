"""Story 75-6 -- a presentation rides the scope cascade, and never duplicates a definition.

WHAT THIS OWNS. One `display` block per (scope, object): how a governed number is
FORMATTED, which colours a white-label client wants beside it, and which filter a
builder pre-fills. Nothing here changes what a number MEANS -- no expression, no
aggregation, no grain, no unit, no predicate applied behind a served value. The
definition row is not read-modified-written, not copied, not touched at all.

WHY IT EXISTS. Measured 2026-09-05, before any code: the cascade
PLATFORM > ORG > PROJECT (`core.metric_semantics._SCOPE_RANK`) carries
DEFINITIONS, and exactly one client-owned presentation value rides beside it --
the client label, `app.dimension_labels` (migration 106), resolved by
`core.dimension_conformance.resolve_dimension_labels`. Format, colour and default
filter had no rail, so an organization wanting its own currency display on a
governed metric had one move: copy the definition into its own scope. That forks
the meaning to change the look.

THE SIBLING, AND WHAT IS COPIED FROM IT. This module is written against the same
cascade shape as `resolve_dimension_labels`: PLATFORM is a BASELINE read off the
object's own definition and never a writable row here; ORG then PROJECT layer over
it; a leaf nobody set is ABSENT rather than invented; and a store that cannot be
reached logs and returns the baseline, because a failure to resolve a presentation
is never a failure to answer.

THE ONE DIFFERENCE FROM THE LABEL RAIL, and it is deliberate: merging is LEAF BY
LEAF, not row by row. An organization that sets `format.currency_display` and a
project that sets `format.decimals` produce ONE format carrying both, and
`sources` says which scope owns which leaf. `default_filter` is the exception and
merges whole -- a field, an operator and a value are one statement, and half of
one is not a filter.

A BASELINE IS NOT AN APPLICATION. The definition's own `format` is what a person
reads as the value they inherit; it is NOT a choice a scope made, so it never
reaches a served figure. Only a leaf whose `sources` entry names ORG or PROJECT
does (`has_client_format`), and what does not reach a slot says so by name rather
than by silence (`application_notes`, carried by the GET payload).

READS THAT ARE ALLOWED TO FAIL RUN INSIDE A SAVEPOINT (`_guarded_read`). Catching
the exception is not enough: a failed statement aborts the whole transaction, so
the CALLER's next statement is the one that raises. A fail-soft that poisons its
caller is a failure with a delay.

CLEARING IS A VERSION. There is no DELETE here. `clear_override` appends a version
with `cleared = true` and an empty display; resolution then behaves as though that
scope had never spoken, and the history still says who cleared it and when.

TRUST CONTRACT (the same S-3 note `metric_semantics` carries): this store has NO
authorization guard by design. Its arguments are assumed ALREADY authorized --
`core/presentation_extends_api.py` owns the org/project guard, exactly as
`dimension_lineage_api` owns it for the label rail.

WHAT IS TO ARBITRATE, and is written down rather than smuggled: the four
`format.kind` members against the six of the spec grammar, the hex `color` that
the persisted Visualization Spec grammar refuses, and the alias table from the
stored free-text `format` to a kind. All three are in the ratified amendment
(`docs/product-architecture/governance.md`, 2026-09-05).

ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from ulid import ULID

from core.audit import declare_action, insert_audit_row

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary. Spelled ONCE here, mirrored by the migration's CHECK constraints.
# ---------------------------------------------------------------------------

SCOPE_PLATFORM = "PLATFORM"
SCOPE_ORG = "ORG"
SCOPE_PROJECT = "PROJECT"

#: The scopes a person may WRITE. PLATFORM is the definition's own scope: it is
#: the baseline of every resolution and is refused by CHECK in migration 348.
WRITABLE_SCOPES: tuple[str, ...] = (SCOPE_ORG, SCOPE_PROJECT)

#: Least -> most specific. The same order `metric_semantics._SCOPE_RANK` gives the
#: definitions; repeated rather than imported because that module is the
#: DEFINITION cascade and this one is the PRESENTATION cascade -- one import
#: between them would make a change to either look like a change to both.
_SCOPE_RANK = {SCOPE_PLATFORM: 0, SCOPE_ORG: 1, SCOPE_PROJECT: 2}

OBJECT_SEMANTIC_CONCEPT = "semantic_concept"
OBJECT_METRIC = "metric"
#: `dimension` is NOT here, and it is a decision: a dimension's client-owned
#: presentation is its NAME, and that already has its rail
#: (`app.dimension_labels`). A dimension carries no number format.
OBJECT_TYPES: tuple[str, ...] = (OBJECT_SEMANTIC_CONCEPT, OBJECT_METRIC)

FORMAT_KINDS: tuple[str, ...] = ("number", "currency", "percent", "duration")
CURRENCY_DISPLAYS: tuple[str, ...] = ("code", "symbol", "name")
FILTER_OPS: tuple[str, ...] = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte")

#: The two operators that take a LIST. Every other operator takes ONE value, and
#: the pairing is checked: `eq` over three values is not one statement a builder
#: can pre-fill, and `in` over a single scalar is a list somebody forgot to write.
LIST_FILTER_OPS: tuple[str, ...] = ("in", "not_in")

MAX_DECIMALS = 6
MAX_FILTER_VALUES = 50
#: A pre-filled value is a value a person reads in a field, not a payload. The
#: bound is the one `object_id` already carries on this rail.
MAX_FILTER_VALUE_LENGTH = 200
MAX_NOTE_LENGTH = 500

#: Six digits, with the hash. Three-digit shorthand and the eight-digit alpha form
#: are refused: a palette a white-label client hands over is written one way, and
#: two spellings of one colour is two colours the day either is compared.
_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")

_COLOUR_LEAVES: tuple[str, ...] = ("series", "positive", "negative")
_FORMAT_LEAVES: tuple[str, ...] = ("kind", "decimals", "compact", "currency_display")
_DISPLAY_KEYS: tuple[str, ...] = ("format", "color", "default_filter")

#: The stored free-text `format` of a definition -> the kind (and the leaves that
#: text also decides). Documented as "to arbitrate (Jean)" in the amendment:
#: `app.metric_definitions.format` was declared free text in migration 049 and is
#: NULL for every migrated platform default.
_FORMAT_TEXT_ALIASES: dict[str, dict[str, Any]] = {
    "currency": {"kind": "currency"},
    "money": {"kind": "currency"},
    "percent": {"kind": "percent"},
    "percentage": {"kind": "percent"},
    "duration": {"kind": "duration"},
    "number": {"kind": "number"},
    "decimal": {"kind": "number"},
    "integer": {"kind": "number", "decimals": 0},
}

ACTION_PRESENTATION_OVERRIDE_SET = declare_action("presentation_override.set")
ACTION_PRESENTATION_OVERRIDE_CLEARED = declare_action("presentation_override.cleared")

_HEAD_PREFIX = "pxo_"
_VERSION_PREFIX = "pxv_"

_HEAD_COLUMNS = (
    "id, org_id, scope_level, project_id, object_type, object_id, "
    "current_version_id, created_by, created_at, updated_at"
)
_VERSION_COLUMNS = (
    "id, override_id, org_id, project_id, version_number, cleared, display, note, "
    "predecessor_version_id, created_by, created_at"
)


class PresentationRefused(ValueError):
    """A named refusal, and every reason at once -- never just the first.

    Mirrors `VisualizationSpecRefused.as_dict()`: a caller that fixes one leaf,
    resubmits and discovers the next has been made to guess.
    """

    def __init__(self, code: str, message: str, refusals: list[dict] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.refusals = list(refusals or [])

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "refusals": self.refusals}


class PresentationNotFound(LookupError):
    """Absent, foreign or denied -- one answer for all three, as every door here."""


def _refusal(code: str, message: str, subject: str, remedy: str) -> dict[str, str]:
    return {"code": code, "message": message, "subject": subject, "remedy": remedy}


def _mint(prefix: str) -> str:
    return f"{prefix}{ULID()}"


# ---------------------------------------------------------------------------
# A. Validation -- PURE, so the grammar is testable without Postgres.
# ---------------------------------------------------------------------------


def validate_display(display: Any) -> dict[str, Any]:
    """Return the display block, or refuse EVERY reason by name.

    A key the block does not declare is REFUSED, never stripped: stripping makes a
    rejected instruction invisible, and the next reader cannot tell a refused
    override from an accepted one (the same reason `visualization_specs` keeps a
    closed allow-list).
    """
    refusals: list[dict] = []
    if not isinstance(display, dict) or not display:
        raise PresentationRefused(
            "empty_display",
            "A presentation override carries a format, a colour or a default filter.",
            [
                _refusal(
                    "empty_display",
                    "Nothing was named to override.",
                    "/display",
                    "Name a format, a colour or a default filter, or clear the override.",
                )
            ],
        )

    for key in display:
        if key not in _DISPLAY_KEYS:
            refusals.append(
                _refusal(
                    "unknown_display_field",
                    f"'{key}' is not part of a presentation override.",
                    f"/display/{key}",
                    "A presentation override carries format, color and default_filter.",
                )
            )

    out: dict[str, Any] = {}
    if "format" in display:
        section = _validate_format(display["format"], refusals)
        if section:
            out["format"] = section
    if "color" in display:
        section = _validate_colour(display["color"], refusals)
        if section:
            out["color"] = section
    if "default_filter" in display:
        section = _validate_default_filter(display["default_filter"], refusals)
        if section:
            out["default_filter"] = section

    if refusals:
        raise PresentationRefused(
            "invalid_display", "This presentation override was refused.", refusals
        )
    if not out:
        raise PresentationRefused(
            "empty_display",
            "A presentation override carries a format, a colour or a default filter.",
            [
                _refusal(
                    "empty_display",
                    "Every section named was empty.",
                    "/display",
                    "Name a format, a colour or a default filter, or clear the override.",
                )
            ],
        )
    return out


def _validate_format(value: Any, refusals: list[dict]) -> dict[str, Any]:
    if not isinstance(value, dict):
        refusals.append(
            _refusal(
                "invalid_format",
                "A format is an object.",
                "/display/format",
                "Send {kind, decimals, compact, currency_display}.",
            )
        )
        return {}
    out: dict[str, Any] = {}
    for key in value:
        if key not in _FORMAT_LEAVES:
            refusals.append(
                _refusal(
                    "unknown_display_field",
                    f"'{key}' is not part of a format.",
                    f"/display/format/{key}",
                    "A format carries kind, decimals, compact and currency_display.",
                )
            )
    if "kind" in value:
        if value["kind"] in FORMAT_KINDS:
            out["kind"] = value["kind"]
        else:
            refusals.append(
                _refusal(
                    "invalid_format_kind",
                    f"'{value['kind']}' is not a format kind.",
                    "/display/format/kind",
                    "Choose one of: " + ", ".join(FORMAT_KINDS) + ".",
                )
            )
    if "decimals" in value:
        decimals = value["decimals"]
        if isinstance(decimals, bool) or not isinstance(decimals, int):
            refusals.append(
                _refusal(
                    "invalid_decimals",
                    "The number of decimals is a whole number.",
                    "/display/format/decimals",
                    f"Send a whole number between 0 and {MAX_DECIMALS}.",
                )
            )
        elif not 0 <= decimals <= MAX_DECIMALS:
            refusals.append(
                _refusal(
                    "invalid_decimals",
                    f"{decimals} decimals is outside the range this product formats.",
                    "/display/format/decimals",
                    f"Send a whole number between 0 and {MAX_DECIMALS}.",
                )
            )
        else:
            out["decimals"] = decimals
    if "compact" in value:
        if isinstance(value["compact"], bool):
            out["compact"] = value["compact"]
        else:
            refusals.append(
                _refusal(
                    "invalid_compact",
                    "Compact is true or false.",
                    "/display/format/compact",
                    "Send true or false.",
                )
            )
    if "currency_display" in value:
        if value["currency_display"] in CURRENCY_DISPLAYS:
            out["currency_display"] = value["currency_display"]
        else:
            refusals.append(
                _refusal(
                    "invalid_currency_display",
                    f"'{value['currency_display']}' is not a way of showing a currency.",
                    "/display/format/currency_display",
                    "Choose one of: " + ", ".join(CURRENCY_DISPLAYS) + ".",
                )
            )
    return out


def _validate_colour(value: Any, refusals: list[dict]) -> dict[str, Any]:
    if not isinstance(value, dict):
        refusals.append(
            _refusal(
                "invalid_color",
                "A colour block is an object.",
                "/display/color",
                "Send {series, positive, negative} with six-digit hex values.",
            )
        )
        return {}
    out: dict[str, Any] = {}
    for key in value:
        if key not in _COLOUR_LEAVES:
            refusals.append(
                _refusal(
                    "unknown_display_field",
                    f"'{key}' is not part of a colour block.",
                    f"/display/color/{key}",
                    "A colour block carries series, positive and negative.",
                )
            )
    for leaf in _COLOUR_LEAVES:
        if leaf not in value:
            continue
        candidate = value[leaf]
        if isinstance(candidate, str) and _HEX_COLOUR.match(candidate):
            out[leaf] = candidate.lower()
        else:
            refusals.append(
                _refusal(
                    "invalid_colour",
                    f"'{candidate}' is not a colour this product can store.",
                    f"/display/color/{leaf}",
                    "Send a six-digit hex colour, for example #1f77b4.",
                )
            )
    return out


def _validate_default_filter(value: Any, refusals: list[dict]) -> dict[str, Any]:
    if not isinstance(value, dict):
        refusals.append(
            _refusal(
                "invalid_default_filter",
                "A default filter is an object.",
                "/display/default_filter",
                "Send {field, op, value}.",
            )
        )
        return {}
    for key in value:
        if key not in ("field", "op", "value"):
            refusals.append(
                _refusal(
                    "unknown_display_field",
                    f"'{key}' is not part of a default filter.",
                    f"/display/default_filter/{key}",
                    "A default filter carries field, op and value.",
                )
            )
    field = value.get("field")
    if not isinstance(field, str) or not field.strip() or len(field) > 200:
        refusals.append(
            _refusal(
                "invalid_filter_field",
                "A default filter names the field it pre-fills.",
                "/display/default_filter/field",
                "Name a canonical field or a governed concept.",
            )
        )
    op = value.get("op")
    if op not in FILTER_OPS:
        refusals.append(
            _refusal(
                "invalid_filter_op",
                f"'{op}' is not an operator a default filter may use.",
                "/display/default_filter/op",
                "Choose one of: " + ", ".join(FILTER_OPS) + ".",
            )
        )
    raw = value.get("value")
    if isinstance(raw, list):
        if op in FILTER_OPS and op not in LIST_FILTER_OPS:
            refusals.append(
                _refusal(
                    "invalid_filter_value_shape",
                    f"'{op}' compares one value, and a list was sent.",
                    "/display/default_filter/value",
                    "Send one value, or choose " + " or ".join(LIST_FILTER_OPS) + ".",
                )
            )
        if not raw or len(raw) > MAX_FILTER_VALUES:
            refusals.append(
                _refusal(
                    "invalid_filter_value",
                    "A list of values holds between 1 and "
                    f"{MAX_FILTER_VALUES} plain values.",
                    "/display/default_filter/value",
                    "Send plain values, never an expression.",
                )
            )
        else:
            for entry in raw:
                _validate_filter_value(entry, refusals)
    else:
        if op in LIST_FILTER_OPS:
            refusals.append(
                _refusal(
                    "invalid_filter_value_shape",
                    f"'{op}' compares a list of values, and one value was sent.",
                    "/display/default_filter/value",
                    "Send a list of values, or choose eq or ne.",
                )
            )
        _validate_filter_value(raw, refusals)
    if refusals:
        return {}
    return {"field": field.strip(), "op": op, "value": raw}


def _validate_filter_value(raw: Any, refusals: list[dict]) -> None:
    """ONE pre-filled value: a bounded string, a real number or a boolean.

    NaN AND THE INFINITIES ARE REFUSED BY NAME. `json.dumps` writes them as the
    bare words `NaN` and `Infinity`, which `jsonb` accepts on the way in and which
    no other JSON reader accepts on the way out -- so a filter carrying one would
    store here and break the screen that reads it back, with no sentence saying
    which value did it.
    """
    if isinstance(raw, bool):
        return
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
            refusals.append(
                _refusal(
                    "invalid_filter_value",
                    "A default filter's value is a real number.",
                    "/display/default_filter/value",
                    "Send a number, never NaN and never an infinity.",
                )
            )
        return
    if isinstance(raw, str):
        if len(raw) > MAX_FILTER_VALUE_LENGTH:
            refusals.append(
                _refusal(
                    "invalid_filter_value",
                    "A pre-filled value is at most "
                    f"{MAX_FILTER_VALUE_LENGTH} characters.",
                    "/display/default_filter/value",
                    f"Shorten it to {MAX_FILTER_VALUE_LENGTH} characters.",
                )
            )
        return
    refusals.append(
        _refusal(
            "invalid_filter_value",
            "A default filter's value is a plain value or a list of them.",
            "/display/default_filter/value",
            "Send a plain value, never an expression.",
        )
    )


def _validate_scope(scope_level: str, project_id: str | None) -> None:
    if scope_level == SCOPE_PLATFORM:
        raise PresentationRefused(
            "platform_scope_forbidden",
            "The platform scope carries the definition and cannot be overridden here.",
            [
                _refusal(
                    "platform_scope_forbidden",
                    "A platform presentation is the definition's own.",
                    "/scope_level",
                    "Set the presentation on the organization or on the project.",
                )
            ],
        )
    if scope_level not in WRITABLE_SCOPES:
        raise PresentationRefused(
            "invalid_scope",
            "A presentation is set on an organization or on a project.",
            [
                _refusal(
                    "invalid_scope",
                    f"'{scope_level}' is not a scope of this rail.",
                    "/scope_level",
                    "Send ORG or PROJECT.",
                )
            ],
        )
    if scope_level == SCOPE_PROJECT and not project_id:
        raise PresentationRefused(
            "missing_project",
            "A project presentation names its project.",
            [
                _refusal(
                    "missing_project",
                    "No project was named.",
                    "/project_id",
                    "Name the project this presentation belongs to.",
                )
            ],
        )
    if scope_level == SCOPE_ORG and project_id:
        raise PresentationRefused(
            "invalid_scope",
            "An organization presentation names no project.",
            [
                _refusal(
                    "invalid_scope",
                    "A project was named on an organization presentation.",
                    "/project_id",
                    "Set the scope to PROJECT, or drop the project.",
                )
            ],
        )


def _validate_object(object_type: str, object_id: str) -> None:
    if object_type not in OBJECT_TYPES:
        raise PresentationRefused(
            "invalid_object_type",
            "A presentation is set on a governed concept or on a metric.",
            [
                _refusal(
                    "invalid_object_type",
                    f"'{object_type}' has no presentation on this rail.",
                    "/object_type",
                    "Send " + " or ".join(OBJECT_TYPES) + ".",
                )
            ],
        )
    if not object_id or not object_id.strip() or len(object_id) > 200:
        raise PresentationRefused(
            "missing_object",
            "A presentation names the object it dresses.",
            [
                _refusal(
                    "missing_object",
                    "No object was named.",
                    "/object_id",
                    "Name the concept or the metric.",
                )
            ],
        )


# ---------------------------------------------------------------------------
# B. The merge -- PURE. Leaf by leaf, and every leaf says where it came from.
# ---------------------------------------------------------------------------


def merge_display_layers(layers: list[tuple[str, dict]]) -> dict[str, Any]:
    """Merge `[(scope, display), ...]` least specific first.

    Returns `{"display": {...}, "sources": {"<dotted leaf>": "<scope>"}}`.
    `format` and `color` merge LEAF BY LEAF; `default_filter` merges WHOLE,
    because a field, an operator and a value are one statement.
    """
    display: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for scope, block in layers:
        if not isinstance(block, dict):
            continue
        for section in ("format", "color"):
            values = block.get(section)
            if not isinstance(values, dict):
                continue
            for leaf, value in values.items():
                display.setdefault(section, {})[leaf] = value
                sources[f"{section}.{leaf}"] = scope
        candidate = block.get("default_filter")
        if isinstance(candidate, dict) and candidate:
            display["default_filter"] = dict(candidate)
            sources["default_filter"] = scope

    return {"display": display, "sources": sources}


def baseline_from_format_text(format_text: str | None) -> dict[str, Any]:
    """The definition's free-text `format` -> a format block, or nothing.

    An unrecognised text yields NOTHING rather than a guess: a definition whose
    `format` reads `bigint` has not declared a presentation, and inventing one
    here would put a format on a screen that no one chose.
    """
    if not format_text or not isinstance(format_text, str):
        return {}
    alias = _FORMAT_TEXT_ALIASES.get(format_text.strip().lower())
    return {"format": dict(alias)} if alias else {}


# ---------------------------------------------------------------------------
# C. The baseline read -- the object's OWN definition, never a row of this rail.
# ---------------------------------------------------------------------------


#: WHERE a concept is reachable from a scope, and it is ONE clause because two
#: would drift. A concept lives in a Project (or nowhere, and then it is the
#: platform's). A PROJECT presentation may only dress a concept of that project;
#: an ORG presentation may dress any concept of any project of that organization,
#: which is exactly what an agency setting one currency display for a client needs.
_CONCEPT_IN_PROJECT = """
    FROM app.semantic_concepts c
    LEFT JOIN app.projects p ON p.id = c.project_id
    WHERE c.id = %s AND (c.project_id IS NULL OR c.project_id = %s)
"""
_CONCEPT_IN_ORG = """
    FROM app.semantic_concepts c
    LEFT JOIN app.projects p ON p.id = c.project_id
    WHERE c.id = %s AND (c.project_id IS NULL OR p.org_id = %s)
"""


def _concept_reach(*, org_id: str, project_id: str | None, object_id: str):
    """`(sql fragment, params)` -- the project clause when there is one, else the org's."""
    if project_id:
        return _CONCEPT_IN_PROJECT, (object_id, project_id)
    return _CONCEPT_IN_ORG, (object_id, org_id)


def _concept_baseline(
    conn, *, org_id: str, project_id: str | None, object_id: str
) -> dict[str, Any]:
    reach, params = _concept_reach(org_id=org_id, project_id=project_id, object_id=object_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.display, v.format "
            "FROM app.semantic_concept_versions v WHERE v.id = ("
            "  SELECT c.current_version_id " + reach + " LIMIT 1)",
            params,
        )
        row = cur.fetchone()
    if row is None:
        return {}
    stored = row[0] if isinstance(row[0], dict) else {}
    baseline = baseline_from_format_text(row[1])
    # A definition that already carries its own display block wins over the text:
    # the block is the explicit statement, the text is the legacy one.
    for section in _DISPLAY_KEYS:
        value = stored.get(section)
        if isinstance(value, dict) and value:
            baseline[section] = dict(value)
    return baseline


#: WHERE a metric definition is reachable from a scope, and it is the SAME shape
#: as `_CONCEPT_IN_*` above. `app.metric_definitions` is itself scoped
#: PLATFORM / ORG / PROJECT, so reachability is stated scope by scope rather than
#: with a COALESCE: `project_id = COALESCE(%s, project_id)` degenerates to
#: `project_id = project_id` on an ORG-scoped request and matches EVERY project's
#: metric of EVERY organization -- which is an existence oracle, not a reach.
_METRIC_IN_PROJECT = """
    FROM app.metric_definitions m
    LEFT JOIN app.projects p ON p.id = m.project_id
    WHERE m.canonical_name = %s
      AND (m.scope_level = 'PLATFORM'
           OR (m.scope_level = 'ORG' AND m.org_id = %s)
           OR (m.scope_level = 'PROJECT' AND m.project_id = %s AND p.org_id = %s))
"""
_METRIC_IN_ORG = """
    FROM app.metric_definitions m
    LEFT JOIN app.projects p ON p.id = m.project_id
    WHERE m.canonical_name = %s
      AND (m.scope_level = 'PLATFORM'
           OR (m.scope_level = 'ORG' AND m.org_id = %s)
           OR (m.scope_level = 'PROJECT' AND p.org_id = %s))
"""

#: `_SCOPE_RANK` spelled for the planner, DERIVED from the dict above so the two
#: cannot drift. It orders the definition rows a scope can reach, most specific
#: first -- the same order `metric_semantics` resolves a definition with.
_METRIC_SCOPE_RANK_SQL = (
    "CASE m.scope_level "
    + " ".join(f"WHEN '{scope}' THEN {rank}" for scope, rank in _SCOPE_RANK.items())
    + " ELSE 0 END DESC"
)


def _metric_reach(*, org_id: str, project_id: str | None, object_id: str):
    """`(sql fragment, params)` -- the project clause when there is one, else the org's."""
    if project_id:
        return _METRIC_IN_PROJECT, (object_id, org_id, project_id, org_id)
    return _METRIC_IN_ORG, (object_id, org_id, org_id)


def _metric_baseline(
    conn, *, org_id: str, project_id: str | None, object_id: str
) -> dict[str, Any]:
    """The definition's own `format`, read at the MOST SPECIFIC scope that carries it.

    Not the PLATFORM row only: `app.metric_definitions` rides the same cascade as
    everything else here, so an organization that already curated its definition
    of `revenue` has that definition's format as its baseline. Reading the
    platform row past an ORG one would show a person a format nobody serves.
    """
    reach, params = _metric_reach(org_id=org_id, project_id=project_id, object_id=object_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.format " + reach + f" ORDER BY {_METRIC_SCOPE_RANK_SQL} LIMIT 1",
            params,
        )
        row = cur.fetchone()
    return baseline_from_format_text(row[0]) if row else {}


def platform_baseline(
    conn, *, org_id: str, project_id: str | None, object_type: str, object_id: str
) -> dict[str, Any]:
    """The display the DEFINITION itself declares. Read-only, always."""
    if object_type == OBJECT_SEMANTIC_CONCEPT:
        return _concept_baseline(
            conn, org_id=org_id, project_id=project_id, object_id=object_id
        )
    if object_type == OBJECT_METRIC:
        return _metric_baseline(
            conn, org_id=org_id, project_id=project_id, object_id=object_id
        )
    return {}


# ---------------------------------------------------------------------------
# D. Resolution.
# ---------------------------------------------------------------------------


def org_of_project(conn, *, project_id: str) -> str | None:
    """The organization a project belongs to, or None.

    IT LIVES HERE AND NOT IN THE ROUTE MODULE, and that is criterion 4 of
    `module-boundaries.md`, not a preference: a `*_api.py` parses, authorizes and
    calls a service -- it executes no SQL. The door needs this one fact to resolve
    the org of a project-scoped request before it asks membership, so the fact is a
    service function like every other read of this rail
    (`scripts/api_sql_census.py --gate` measures it).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _load_override_layers(
    conn, *, org_id: str, project_id: str | None, object_type: str, object_id: str
) -> list[tuple[str, dict]]:
    """`[(scope, display)]` for this org and this project, least specific first.

    A CLEARED head contributes NOTHING -- that is what clearing means -- and it is
    read as a version rather than inferred from an absent row, so the history
    stays legible.
    """
    clauses = ["(h.scope_level = 'ORG' AND h.org_id = %s)"]
    params: list[Any] = [org_id]
    if project_id:
        clauses.append("(h.scope_level = 'PROJECT' AND h.project_id = %s)")
        params.append(project_id)
    params.extend([object_type, object_id])

    sql = f"""
        SELECT h.scope_level, v.cleared, v.display
        FROM app.presentation_overrides h
        JOIN app.presentation_override_versions v ON v.id = h.current_version_id
        WHERE ({" OR ".join(clauses)})
          AND h.object_type = %s AND h.object_id = %s
    """
    rows: list[tuple[str, bool, Any]] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    layers: list[tuple[str, dict]] = []
    for scope, cleared, display in sorted(rows, key=lambda r: _SCOPE_RANK.get(r[0], 0)):
        if cleared:
            continue
        layers.append((str(scope), display if isinstance(display, dict) else {}))
    return layers


#: The markers the two fail-soft reads set. Fixed names rather than minted ones: a
#: nested SAVEPOINT of the same name hides the outer one and `ROLLBACK TO` reaches
#: the most recent, which is exactly the behaviour a re-entrant read wants.
_OVERRIDE_READ_SAVEPOINT = "presentation_override_read"
_RENDER_READ_SAVEPOINT = "presentation_render_read"
_PINNED_READ_SAVEPOINT = "presentation_pinned_read"


def _guarded_read(conn, *, marker: str, subject: str, read, fallback):
    """Run `read()` inside a SAVEPOINT; on any failure log, UNWIND, answer `fallback`.

    THE DEFECT THIS EXISTS FOR, and it is one defect in two places. Catching the
    exception is not enough: a statement that fails in PostgreSQL aborts the whole
    transaction, so the caller's NEXT statement -- not this one -- is the one that
    raises, with a sentence about `current transaction is aborted` naming nothing
    a person did. A fail-soft that poisons the caller is not fail-soft; it is a
    failure with a delay. The marker is set before the read and rolled back to on
    the way out, so the render that called this can go on composing its figure.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {marker}")
    except Exception as exc:  # noqa: BLE001 -- no marker means no safe read
        logger.warning(
            "presentation_extends: %s not attempted: %s", subject, type(exc).__name__
        )
        return fallback
    try:
        answer = read()
    except Exception as exc:  # noqa: BLE001 -- the baseline is still a true answer
        logger.warning("presentation_extends: %s failed: %s", subject, type(exc).__name__)
        try:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                cur.execute(f"RELEASE SAVEPOINT {marker}")
        except Exception as unwind:  # noqa: BLE001 -- nothing left to repair here
            logger.warning(
                "presentation_extends: transaction not recovered: %s",
                type(unwind).__name__,
            )
        return fallback
    try:
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {marker}")
    except Exception as exc:  # noqa: BLE001 -- the rows were read; the marker is spent
        logger.warning(
            "presentation_extends: savepoint not released: %s", type(exc).__name__
        )
    return answer


def _load_override_layers_fail_soft(
    conn, *, org_id: str, project_id: str | None, object_type: str, object_id: str
) -> list[tuple[str, dict]]:
    """The override read, INSIDE A SAVEPOINT, so a failure leaves a usable transaction."""
    return _guarded_read(
        conn,
        marker=_OVERRIDE_READ_SAVEPOINT,
        subject=f"override load org={org_id} object={object_id}",
        read=lambda: _load_override_layers(
            conn,
            org_id=org_id,
            project_id=project_id,
            object_type=object_type,
            object_id=object_id,
        ),
        fallback=[],
    )


def resolve_display(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    object_type: str,
    object_id: str,
) -> dict[str, Any]:
    """`{"display": {...}, "sources": {leaf: scope}}` for one object in one scope.

    FAIL-SOFT ON THE OVERRIDE READ, exactly as the client label rail is: a store
    that cannot answer logs and yields the definition's own baseline, because a
    failure to resolve a presentation is never a failure to answer -- and it
    leaves the caller's transaction USABLE, which is what the SAVEPOINT in
    `_load_override_layers_fail_soft` is for. The BASELINE read is not caught --
    a definition that cannot be read is the caller's problem, not a
    presentation's.
    """
    _validate_object(object_type, object_id)
    baseline = platform_baseline(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_type=object_type,
        object_id=object_id,
    )
    layers = _load_override_layers_fail_soft(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_type=object_type,
        object_id=object_id,
    )
    return merge_display_layers([(SCOPE_PLATFORM, baseline), *layers])


def read_override(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    object_type: str,
    object_id: str,
) -> dict[str, Any] | None:
    """The row stored AT one exact scope -- the curation surface's own read."""
    _validate_scope(scope_level, project_id)
    _validate_object(object_type, object_id)
    head = _select_head(
        conn,
        org_id=org_id,
        project_id=project_id,
        scope_level=scope_level,
        object_type=object_type,
        object_id=object_id,
    )
    if head is None:
        return None
    version = _select_version(conn, version_id=head.get("current_version_id"))
    return {"override": head, "version": version}


def history(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    object_type: str,
    object_id: str,
) -> list[dict[str, Any]]:
    """Every version of one scope's presentation, newest first. A clear is one of them."""
    _validate_scope(scope_level, project_id)
    _validate_object(object_type, object_id)
    head = _select_head(
        conn,
        org_id=org_id,
        project_id=project_id,
        scope_level=scope_level,
        object_type=object_type,
        object_id=object_id,
    )
    if head is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_VERSION_COLUMNS}
            FROM app.presentation_override_versions
            WHERE override_id = %s
            ORDER BY version_number DESC
            """,
            (head["id"],),
        )
        cols = [desc[0] for desc in cur.description]
        return [_row(cols, row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# E. Writes. One transaction each; the caller owns the connection.
# ---------------------------------------------------------------------------

_TS_COLS = frozenset({"created_at", "updated_at"})


def _row(cols, row) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for col, value in zip(cols, row):
        record[col] = value.isoformat() if col in _TS_COLS and value is not None else value
    return record


def _select_head(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    object_type: str,
    object_id: str,
    for_update: bool = False,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_HEAD_COLUMNS}
            FROM app.presentation_overrides
            WHERE scope_level = %s AND org_id = %s
              AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND object_type = %s AND object_id = %s
            {"FOR UPDATE" if for_update else ""}
            """,
            (scope_level, org_id, project_id, object_type, object_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row(cols, row)


def _select_version(conn, *, version_id: str | None) -> dict[str, Any] | None:
    if not version_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_VERSION_COLUMNS} FROM app.presentation_override_versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row(cols, row)


def _assert_object_exists(
    conn, *, org_id: str, project_id: str | None, object_type: str, object_id: str
) -> None:
    """The object must be reachable from this scope. No foreign key can say it.

    `object_id` names one of two tables depending on `object_type`, so the
    reference is checked HERE, at the door, exactly as a Dossier version's Render
    references are (migration 340's header). An object the caller cannot reach
    answers the same way an absent one does.

    BOTH BRANCHES ASK THE SAME QUESTION OF THE SAME FRAGMENT the baseline read
    uses -- `_concept_reach` and `_metric_reach`. Two spellings of "reachable"
    would drift, and the metric one already had: `project_id = COALESCE(%s,
    project_id)` is `project_id = project_id` when no project is named, so an ORG
    request reached every project's metric of every organization.
    """
    reach, params = (
        _concept_reach(org_id=org_id, project_id=project_id, object_id=object_id)
        if object_type == OBJECT_SEMANTIC_CONCEPT
        else _metric_reach(org_id=org_id, project_id=project_id, object_id=object_id)
    )
    with conn.cursor() as cur:
        cur.execute("SELECT 1 " + reach + " LIMIT 1", params)
        if cur.fetchone() is None:
            raise PresentationNotFound("no such object in this scope")


def _append_version(
    conn,
    *,
    head: dict[str, Any],
    display: dict[str, Any],
    cleared: bool,
    note: str | None,
    identity: str,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version_number
            FROM app.presentation_override_versions
            WHERE override_id = %s
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (head["id"],),
        )
        previous = cur.fetchone()
    predecessor_id = previous[0] if previous else None
    next_number = (previous[1] if previous else 0) + 1

    version_id = _mint(_VERSION_PREFIX)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.presentation_override_versions
                (id, override_id, org_id, project_id, version_number, cleared,
                 display, note, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING {_VERSION_COLUMNS}
            """,
            (
                version_id,
                head["id"],
                head["org_id"],
                head["project_id"],
                next_number,
                cleared,
                json.dumps(display),
                note,
                predecessor_id,
                identity,
            ),
        )
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description]
        cur.execute(
            "UPDATE app.presentation_overrides SET current_version_id = %s WHERE id = %s",
            (version_id, head["id"]),
        )
    return _row(cols, row)


def _ensure_head(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    object_type: str,
    object_id: str,
    identity: str,
) -> dict[str, Any]:
    head = _select_head(
        conn,
        org_id=org_id,
        project_id=project_id,
        scope_level=scope_level,
        object_type=object_type,
        object_id=object_id,
        for_update=True,
    )
    if head is not None:
        return head
    head_id = _mint(_HEAD_PREFIX)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.presentation_overrides
                (id, org_id, scope_level, project_id, object_type, object_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {_HEAD_COLUMNS}
            """,
            (head_id, org_id, scope_level, project_id, object_type, object_id, identity),
        )
        row = cur.fetchone()
        cols = [desc[0] for desc in cur.description]
    return _row(cols, row)


def set_override(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    object_type: str,
    object_id: str,
    display: Any,
    identity: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Append a version carrying `display`, and point the head at it.

    ONE TRANSACTION, and it is the CALLER's: the head, the version and the audit
    row commit together or not at all. A refused leaf writes nothing -- validation
    runs before the first INSERT.
    """
    _validate_scope(scope_level, project_id)
    _validate_object(object_type, object_id)
    validated = validate_display(display)
    if note is not None and len(note) > MAX_NOTE_LENGTH:
        raise PresentationRefused(
            "note_too_long",
            f"A note is at most {MAX_NOTE_LENGTH} characters.",
            [
                _refusal(
                    "note_too_long",
                    "The note is longer than this rail stores.",
                    "/note",
                    f"Shorten it to {MAX_NOTE_LENGTH} characters.",
                )
            ],
        )
    _assert_object_exists(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_type=object_type,
        object_id=object_id,
    )
    head = _ensure_head(
        conn,
        org_id=org_id,
        project_id=project_id,
        scope_level=scope_level,
        object_type=object_type,
        object_id=object_id,
        identity=identity,
    )
    version = _append_version(
        conn, head=head, display=validated, cleared=False, note=note, identity=identity
    )
    insert_audit_row(
        conn,
        identity=identity,
        action=ACTION_PRESENTATION_OVERRIDE_SET,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "project_id": project_id,
            "scope_level": scope_level,
            "object_type": object_type,
            "object_id": object_id,
            "override_id": head["id"],
            "version_id": version["id"],
            "fields": sorted(validated.keys()),
        },
    )
    return {"override": head, "version": version}


def clear_override(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    scope_level: str,
    object_type: str,
    object_id: str,
    identity: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Append a CLEARED version. Never a DELETE.

    The scope stops speaking and the parent becomes visible again -- and the
    ledger still says who stopped it and when, which a DELETE cannot.
    """
    _validate_scope(scope_level, project_id)
    _validate_object(object_type, object_id)
    head = _select_head(
        conn,
        org_id=org_id,
        project_id=project_id,
        scope_level=scope_level,
        object_type=object_type,
        object_id=object_id,
        for_update=True,
    )
    if head is None:
        raise PresentationNotFound("no presentation is stored at that scope")
    current = _select_version(conn, version_id=head.get("current_version_id"))
    if current is not None and current.get("cleared"):
        return {"override": head, "version": current}
    version = _append_version(
        conn, head=head, display={}, cleared=True, note=note, identity=identity
    )
    insert_audit_row(
        conn,
        identity=identity,
        action=ACTION_PRESENTATION_OVERRIDE_CLEARED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "project_id": project_id,
            "scope_level": scope_level,
            "object_type": object_type,
            "object_id": object_id,
            "override_id": head["id"],
            "version_id": version["id"],
        },
    )
    return {"override": head, "version": version}


# ---------------------------------------------------------------------------
# F. The ONE render consumer.
#
# `visualization-spec.v1` has ONE format slot and it is document-level:
# `formatting.number_style` (`core/visualization_specs.py:429`). There is no
# per-member format slot and no colour literal slot -- the grammar refuses a hex
# in any string leaf, because `visualization-and-rendering.md:288` gives semantic
# colours and themes to the runtime. So this applies FORMAT only, and the colour
# arbitration is written in the amendment rather than forced through a slot that
# was designed to refuse it.
# ---------------------------------------------------------------------------

#: `format.kind` (+ its digit leaves) -> the grammar's `number_style`. `duration`
#: has no member in that enum and is deliberately absent: applying `decimal` to a
#: duration would print a wrong number, and applying nothing prints today's.
_NUMBER_STYLE_DEFAULT = "auto"


def number_style_for(display: dict[str, Any]) -> str | None:
    """The `formatting.number_style` this display asks for, or None. PURE."""
    fmt = display.get("format") if isinstance(display, dict) else None
    if not isinstance(fmt, dict):
        return None
    kind = fmt.get("kind")
    if kind == "currency":
        return "currency"
    if kind == "percent":
        return "percent"
    if kind == "number":
        if fmt.get("compact") is True:
            return "compact"
        if fmt.get("decimals") == 0:
            return "integer"
        return "decimal"
    # `duration` and an absent kind: no slot, nothing applied.
    return None


def has_client_format(sources: Any) -> bool:
    """True when a `format` leaf was set by an ORGANIZATION or a PROJECT. PURE.

    THE LINE BETWEEN "THE DEFINITION SAYS SO" AND "SOMEBODY CHOSE". The platform
    baseline is the definition's own free-text `format` read through the alias
    table; turning THAT into a `formatting.number_style` would change the served
    document of every figure whose measure carries `format = 'currency'`, with no
    override stored anywhere -- and the amendment's own `Incomplete if` refuses
    exactly that: "a render that has no override for any of its measures is served
    a spec document that differs, in any byte, from the stored one".
    """
    if not isinstance(sources, dict):
        return False
    return any(
        isinstance(leaf, str) and leaf.startswith("format.") and scope in WRITABLE_SCOPES
        for leaf, scope in sources.items()
    )


# ---------------------------------------------------------------------------
# G. What of a resolved block actually reaches a render slot -- said out loud.
# ---------------------------------------------------------------------------

#: The sentences the GET payload carries beside each section, so a reader of the
#: API never has to infer from silence whether a value they stored is served.
#: They are the amendment's arbitrations, in the words a person reads.
_NOT_APPLIED_COLOR = (
    "Colour is stored and read back here. No figure slot accepts a colour: the "
    "palette belongs to the runtime theme, and the persisted figure grammar "
    "refuses a hex value. Until that arbitration is made, nothing is applied."
)
_NOT_APPLIED_DEFAULT_FILTER = (
    "A default filter pre-fills a builder when a person opens a new question. No "
    "render path applies it, and none may: narrowing a served number behind the "
    "reader's back would change the answer, which belongs to the question."
)
_NOT_APPLIED_METRIC = (
    "The figure path resolves the governed concepts a question pins, so a format "
    "set on a metric definition is stored and read back here and reaches no "
    "figure. Set it on the governed concept the question measures."
)
_NOT_APPLIED_NO_OVERRIDE = (
    "Nothing is set on this organization or project, so a figure keeps the format "
    "the definition already carries."
)
_NOT_APPLIED_NO_SLOT = (
    "This format has no style in the figure grammar -- a duration is printed by "
    "the runtime, not by a number style -- so nothing is applied."
)
_APPLIED_FORMAT = (
    "A figure that measures this concept is served with this format, unless the "
    "figure's author already chose one. A question compiled across two sources "
    "names its measures by field and is not dressed by this rail."
)


def application_notes(*, object_type: str, resolved: dict[str, Any]) -> dict[str, Any]:
    """Per section: is it applied to a served figure, and if not, WHY. PURE.

    A resolved block that says only what it holds lets a reader believe a colour
    they stored is on their charts. Three of the four answers here are `false`,
    and each names the reason in the words of the amendment rather than leaving
    the caller to discover it by looking at a figure.
    """
    display = resolved.get("display") if isinstance(resolved, dict) else None
    sources = resolved.get("sources") if isinstance(resolved, dict) else None
    display = display if isinstance(display, dict) else {}

    if not has_client_format(sources):
        fmt = {"applied": False, "reason": _NOT_APPLIED_NO_OVERRIDE}
    elif object_type != OBJECT_SEMANTIC_CONCEPT:
        fmt = {"applied": False, "reason": _NOT_APPLIED_METRIC}
    elif number_style_for(display) is None:
        fmt = {"applied": False, "reason": _NOT_APPLIED_NO_SLOT}
    else:
        fmt = {"applied": True, "reason": _APPLIED_FORMAT}

    return {
        "format": fmt,
        "color": {"applied": False, "reason": _NOT_APPLIED_COLOR},
        "default_filter": {"applied": False, "reason": _NOT_APPLIED_DEFAULT_FILTER},
    }


def apply_display_to_document(
    conn,
    *,
    document: Any,
    org_id: str,
    project_id: str | None,
    member_ids: list[str],
) -> Any:
    """Return the document to SERVE: today's, or today's with the agreed style.

    FOUR THINGS IT WILL NOT DO, and each is an `Incomplete if` of the amendment:
    it does not rewrite the stored Visualization Spec version (its `content_hash`
    does not move -- what is extended is the copy handed to the runtime); it does
    not overwrite a `number_style` the author actually chose (a stored intent
    outranks a scope preference); it does not fail a render, ever -- a
    presentation that cannot be resolved yields the document unchanged, in a
    transaction the render can go on using; and IT MOVES NOTHING WHEN NOBODY
    OVERRODE ANYTHING -- `has_client_format` is the whole of that fourth rule.
    The definition's own free-text `format` is a BASELINE a person reads, not a
    choice a scope made, and serving it as a `number_style` would make every
    figure whose measure says `currency` differ from the document stored for it.

    When the measures of one figure disagree on a style, nothing is applied: a
    document carries ONE number style, and picking one measure's over another's
    would be the product choosing which number is read correctly.

    ONLY GOVERNED CONCEPTS ARE RESOLVED HERE, and that is written in the
    amendment rather than left to be discovered: a figure's members are the
    concepts its pinned question measures (`sc_...`), a `metric` override names
    `app.metric_definitions.canonical_name`, and a question compiled across two
    sources names its measures by field (`m_<field>`). Neither of the last two
    reaches a figure through this rail, and the GET payload says so per section
    (`application_notes`) instead of leaving a stored value looking served.
    """
    if not isinstance(document, dict) or not member_ids:
        return document
    formatting = document.get("formatting")
    stored_style = (
        formatting.get("number_style") if isinstance(formatting, dict) else None
    )
    if stored_style not in (None, _NUMBER_STYLE_DEFAULT):
        return document

    def _styles() -> set[str]:
        found: set[str] = set()
        for member_id in member_ids:
            resolved = resolve_display(
                conn,
                org_id=org_id,
                project_id=project_id,
                object_type=OBJECT_SEMANTIC_CONCEPT,
                object_id=member_id,
            )
            if not has_client_format(resolved["sources"]):
                continue
            style = number_style_for(resolved["display"])
            if style is not None:
                found.add(style)
        return found

    styles = _guarded_read(
        conn,
        marker=_RENDER_READ_SAVEPOINT,
        subject="display resolution for render",
        read=_styles,
        fallback=set(),
    )

    if len(styles) != 1:
        return document
    style = styles.pop()
    if style == stored_style:
        return document
    served = dict(document)
    served["formatting"] = {**(formatting if isinstance(formatting, dict) else {}),
                            "number_style": style}
    return served


def apply_presentation_extends(
    conn,
    *,
    document: Any,
    org_id: str,
    project_id: str,
    query_spec_version_id: str,
) -> Any:
    """The render path's one call: resolve the PINNED measures, extend the document.

    The members come from `visualization_specs.load_pinned_query_spec_version` --
    the one reader of `app.query_spec_versions` this epic is allowed to have. A
    second reader would be a second opinion on what a member's role is.
    """
    from core.visualization_families import ROLE_MEASURE  # noqa: PLC0415
    from core.visualization_specs import (  # noqa: PLC0415
        load_pinned_query_spec_version,
    )

    # The same guard as every other read of this rail: an absent or unreadable pin
    # never breaks a render, AND never leaves the render's transaction aborted.
    pinned = _guarded_read(
        conn,
        marker=_PINNED_READ_SAVEPOINT,
        subject="pinned members, document served as stored",
        read=lambda: load_pinned_query_spec_version(
            conn, project_id=project_id, query_spec_version_id=query_spec_version_id
        ),
        fallback=None,
    )
    if pinned is None:
        return document
    member_ids = [
        member_id for member_id, role in pinned.roles.items() if role == ROLE_MEASURE
    ]
    return apply_display_to_document(
        conn,
        document=document,
        org_id=org_id,
        project_id=project_id,
        member_ids=member_ids,
    )
