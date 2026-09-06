"""Story 62.1 -- the MMM extract: the daily fact, long, with its provenance.

WHAT THIS IS AND WHAT IT IS NOT. It is a READ. It composes objects that already
exist -- a published Semantic View, the governed physical plan behind it, the MDM
measurement grain that sanctions a cut, the money authority that says which
column is an amount -- and returns rows. It builds no dataset, writes no table,
lands no file in a bucket and mints no row of its own. The plan of epic 62 asked
for "un dataset et des vues BigQuery dedies" and "fichiers CSV/Parquet sur GCS";
`docs/product-architecture/capabilities/analytics-alignment.md` refuses exactly
that four paragraphs above the section this story writes into -- *"a request to
copy the mart out of BigQuery (`destination`, `gcs_uri`, a third-party DBMS)"* --
and the ratified document is twenty days younger than the plan. The arbitration
is recorded in that document, at the end of its section 4, and this module is
what it describes.

WHY NOT READ `fact_daily_kpi` DIRECTLY, which is where the daily grain lives. A
file read off the mart cannot say, line by line, which Semantic View version,
which Concept version and which Datastream relation produced it -- and an MMM
feeds a model whose coefficients nobody can re-derive later without exactly that.
So the extract resolves the SAME plan a governed query resolves
(`query_execution.resolve_physical_plan`), which means every refusal that path
already names travels here unchanged: an inactive binding, a member bound to no
Datastream, a request crossing two Datastreams, a run that landed nowhere
readable, and -- since AI-342 -- a dimension the bound relation does not publish.
This module adds no second opinion about any of them.

THE FOUR GATES, IN THIS ORDER, AND THE ORDER IS THE ANSWER.

  1. the View version is PUBLISHED for this Project. A draft is a promise nobody
     has made yet, and a file taken from one would outlive the draft.
  2. every metric is cut by the SAME dimensions, and one live measurement grain
     version sanctions that cut -- `analytics_alignment_read.sanction_breakdown`,
     asked once per metric with the same list. A file holding `spend` by
     (day, channel) beside `conversions` by (day) alone would sum two different
     measurements down one `value` column, and no coefficient read off it means
     anything. The sanctioning version id rides on every row, never only in a
     header.
  3. the physical plan resolves. Gate 3 is after gate 2 deliberately: "no
     governed grain relates these dimensions to this metric" is a fact about the
     declaration and stays true whatever the warehouse answers, while "this
     relation does not publish that breakdown" is a fact about one landing. The
     declaration is the repair a person can make without waiting for a run.
  4. every monetary metric has EXACTLY ONE declared currency over the window.
     Fails closed on zero and on two, in `currency_refusal`'s own two words.

AMOUNTS LEAVE NATIVE, AND THE FILE SAYS SO. The relation a View binds is a
landing, and by `capabilities/currency-fx.md` the source currency stays at
staging while the conversion happens once at read, in `fact_daily_kpi`. An
extract taken from the landing is therefore unconverted. It does not print a
reporting currency it did not apply -- `money_provenance_columns` argues that
point at length and this module obeys it -- it prints the currency the source
reported and names it on every row and in the provenance.

A MISSING DAY IS NAMED AND NEVER SILENT. « une semaine manquante change le
coefficient d'un modele sans que personne ne le voie » is the epic's own
sentence. The provenance lists, per metric, every calendar day of the requested
window that carries no row. It REPORTS them and does not refuse: a campaign that
did not run on a Sunday is a fact about the world, and refusing the file would be
refusing the data instead of describing it.

TRUNCATION IS REFUSED, NEVER PERFORMED. `query_execution.MAX_INLINE_ROWS` bounds
what an immutable Result stores; this read stores nothing, so it declares its own
bound and refuses above it with the gesture that fixes it. A truncated extract is
a file a person will treat as complete -- the rule `datastream_sample_api`
already applies to its own dataset export.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounds. Each one carries the precedent it comes from.
# ---------------------------------------------------------------------------

#: Rows the warehouse may return for one extract, BEFORE the melt into long
#: form. Deliberately not `query_execution.MAX_INLINE_ROWS = 1_000`: that is the
#: ceiling on what an immutable Result keeps inline, and three years of daily
#: rows cut by a handful of channels is the ordinary size of this file. The same
#: distinction `core.model_channel` spells out when it refuses to reuse that
#: constant as a transport budget.
MAX_SOURCE_ROWS = 50_000

#: Days one extract may span. Ten years of daily rows -- wider than any media
#: history a model is fitted on, and narrow enough that the date-gap listing
#: below is bounded by construction.
MAX_WINDOW_DAYS = 3_660

#: Missing days listed by name before the rest is stated as a count. A model
#: owner needs to SEE the shape of the hole; they do not need three thousand
#: dates in a payload.
MAX_LISTED_GAPS = 200

#: What a metric that is not money prints in the `currency` column. An empty
#: string and not `None`: a CSV cell and a JSON value must agree, and `null`
#: rendered by a naive writer becomes the four letters `None`.
NO_CURRENCY = ""


# ---------------------------------------------------------------------------
# Refusals. A code a machine reads, a sentence a person reads, and the gesture
# that repairs it -- the shape `analytics_alignment_read` already uses.
# ---------------------------------------------------------------------------

REFUSAL_VIEW_NOT_FOUND = "semantic_view_version_not_found"
REFUSAL_VIEW_NOT_PUBLISHED = "semantic_view_version_not_published"
REFUSAL_NO_MEASURE_ASKED = "no_metric_asked"
REFUSAL_MEMBER_NOT_IN_VIEW = "member_not_in_semantic_view"
REFUSAL_NO_DAILY_GRAIN = "no_daily_grain_in_semantic_view"
REFUSAL_SEVERAL_DAILY_GRAINS = "several_daily_grains_in_semantic_view"
REFUSAL_WINDOW_MALFORMED = "window_is_not_two_calendar_days"
REFUSAL_WINDOW_TOO_WIDE = "window_too_wide"
REFUSAL_NOT_A_CANONICAL_FIELD = "member_not_in_canonical_registry"
REFUSAL_PLAN_UNAVAILABLE = "physical_plan_unavailable"
REFUSAL_TOO_MANY_ROWS = "extract_larger_than_one_file"
REFUSAL_WAREHOUSE_UNREADABLE = "warehouse_unreadable"


class MmmExportRefused(Exception):
    """A named refusal. `code` for a machine, `message` and `gesture` for a person."""

    def __init__(self, code: str, message: str, gesture: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.gesture = gesture
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "gesture": self.gesture,
            **self.detail,
        }


# ---------------------------------------------------------------------------
# The published View, and the members it carries.
# ---------------------------------------------------------------------------

_VIEW_VERSION = """
    SELECT v.id, v.view_id, v.status, v.version_number, v.name, v.content_hash
      FROM app.semantic_view_versions v
      JOIN app.semantic_views s ON s.id = v.view_id
     WHERE v.id = %(view_version_id)s
       AND v.project_id = %(project_id)s
       AND s.project_id = %(project_id)s
"""

_VIEW_MEMBERS = """
    SELECT vc.concept_id, vc.concept_version_id, vc.role, cv.name, cv.value_type,
           cv.unit, cv.aggregation
      FROM app.semantic_view_version_concepts vc
      JOIN app.semantic_concept_versions cv ON cv.id = vc.concept_version_id
     WHERE vc.view_version_id = %(view_version_id)s
       AND cv.project_id = %(project_id)s
     ORDER BY vc.ordinal
"""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _load_view(conn, *, project_id: str, semantic_view_version_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            _VIEW_VERSION,
            {"view_version_id": semantic_view_version_id, "project_id": project_id},
        )
        row = cur.fetchone()
    if row is None:
        raise MmmExportRefused(
            REFUSAL_VIEW_NOT_FOUND,
            "This Project has no Semantic View version under that address.",
            "Open Governance > Semantic Model, choose the View to extract, and "
            "take the id of its published version.",
        )
    status = _text(row[2])
    if status != "published":
        # A DRAFT IS NOT A PROMISE. Extracting one produces a file whose meaning
        # can change before anybody has agreed to it, and a model fitted on it
        # would be fitted on a definition nobody signed.
        raise MmmExportRefused(
            REFUSAL_VIEW_NOT_PUBLISHED,
            f"This Semantic View version is {status}, not published, so what its "
            "members mean has not been agreed yet.",
            "Publish this Semantic View version, then extract from it. A draft "
            "can still change under a file somebody has already modelled on.",
            semantic_view_version_status=status,
        )
    return {
        "semantic_view_version_id": _text(row[0]),
        "semantic_view_id": _text(row[1]),
        "status": status,
        "version_number": row[3],
        "name": _text(row[4]),
        "content_hash": _text(row[5]),
    }


def _load_members(conn, *, project_id: str, semantic_view_version_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            _VIEW_MEMBERS,
            {"view_version_id": semantic_view_version_id, "project_id": project_id},
        )
        rows = cur.fetchall()
    return [
        {
            "concept_id": _text(row[0]),
            "concept_version_id": _text(row[1]),
            "role": _text(row[2]),
            "name": _text(row[3]),
            "value_type": _text(row[4]),
            "unit": _text(row[5]),
            "aggregation": row[6],
        }
        for row in rows
    ]


def _pick(members: Sequence[Mapping[str, Any]], names: Sequence[str], role: str) -> list[dict]:
    """The members named, in the order asked for. A name the View does not carry refuses."""
    by_name = {member["name"]: member for member in members if member["role"] == role}
    picked: list[dict[str, Any]] = []
    for name in names:
        member = by_name.get(name)
        if member is None:
            raise MmmExportRefused(
                REFUSAL_MEMBER_NOT_IN_VIEW,
                f"This Semantic View publishes no {role} called `{name}`. It "
                f"publishes {_listed(sorted(by_name)) or 'none'}.",
                f"Ask for one of the {role}s this View publishes, or add `{name}` "
                "to the View and publish it again.",
                member=name,
                role=role,
                available=sorted(by_name),
            )
        picked.append(dict(member))
    return picked


def _listed(names: Sequence[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _daily_member(members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The day. Found, never chosen by the caller, and never assumed.

    An MMM extract has ONE time grain and it is the day -- the product has no
    hourly grain anywhere. So the caller does not name the date member: it is the
    View's own dimension whose Concept version declares `value_type = 'date'`.
    Zero of them and the View cannot answer a daily question at all; two of them
    and which day the file is cut on is a decision, which this refuses to take in
    silence.
    """
    days = [
        member
        for member in members
        if member["role"] == "dimension" and member["value_type"] == "date"
    ]
    if not days:
        raise MmmExportRefused(
            REFUSAL_NO_DAILY_GRAIN,
            "This Semantic View publishes no member that is a calendar day, so "
            "nothing in it can carry the daily grain a mix model is fitted on.",
            "Publish a dimension Concept whose value type is `date` in this View "
            "and bind it to the Datastream that reports the day.",
        )
    if len(days) > 1:
        raise MmmExportRefused(
            REFUSAL_SEVERAL_DAILY_GRAINS,
            "This Semantic View publishes "
            + _listed([member["name"] for member in days])
            + " as calendar days, so which one the file is cut on is a decision.",
            "Publish one day member in this View, or split it so each View "
            "carries the single day its Datastreams report.",
            candidates=[member["name"] for member in days],
        )
    return dict(days[0])


# ---------------------------------------------------------------------------
# The window.
# ---------------------------------------------------------------------------


def _window(start: Any, end: Any) -> tuple[date, date]:
    """Two calendar days, in order, within the span one file may carry."""
    try:
        first = date.fromisoformat(_text(start))
        last = date.fromisoformat(_text(end))
    except ValueError as exc:
        raise MmmExportRefused(
            REFUSAL_WINDOW_MALFORMED,
            "An extract covers a range of calendar days, and this range is not "
            f"two of them ({exc}).",
            "Give the first and last day of the window as calendar days, "
            "`YYYY-MM-DD`. There is no hourly grain to ask for.",
        ) from exc
    if last < first:
        raise MmmExportRefused(
            REFUSAL_WINDOW_MALFORMED,
            f"The window ends ({last.isoformat()}) before it starts "
            f"({first.isoformat()}).",
            "Give the first day, then the last.",
        )
    span = (last - first).days + 1
    if span > MAX_WINDOW_DAYS:
        raise MmmExportRefused(
            REFUSAL_WINDOW_TOO_WIDE,
            f"This window covers {span} days, and one extract carries at most "
            f"{MAX_WINDOW_DAYS}.",
            "Narrow the window, and take the earlier years as their own file.",
            days=span,
            maximum=MAX_WINDOW_DAYS,
        )
    return first, last


def _days(first: date, last: date) -> list[str]:
    out: list[str] = []
    cursor = first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Gate 2 -- the governed grain sanctions the cut, once per metric.
# ---------------------------------------------------------------------------


def _canonical_ids(conn, *, project_id: str, names: Sequence[str]) -> dict[str, str]:
    """Concept name -> canonical field id, through the registry and nothing else.

    THE BRIDGE IS THE NAME, AND IT IS THE ONE THE WHOLE PRODUCT ALREADY RUNS ON.
    A measurement grain names `app.mdm_canonical_fields` rows; a Semantic View
    names Concepts. `query_execution.resolve_physical_plan` already joins the two
    vocabularies by equality on the name -- *"canonical_target is the mapping's
    own name for the field; the semantic concept carries the same name. That
    equality is the join"* -- and `metric_dimensions` says the same of the
    landing's breakdown key. So nothing new is invented here; the same equality is
    asked one layer up, through `list_visible_canonical_fields`, which is the
    predicate every binding is accepted against.

    A name with no canonical field is REFUSED and never silently skipped: a
    metric that skipped this gate would reach the file with no grain behind it.
    """
    from core.canonical_field_registry import list_visible_canonical_fields  # noqa: PLC0415

    registry = {
        _text(field.get("canonical_name")): _text(field.get("id"))
        for field in list_visible_canonical_fields(conn, project_id=project_id)
    }
    resolved: dict[str, str] = {}
    for name in names:
        field_id = registry.get(name)
        if not field_id:
            raise MmmExportRefused(
                REFUSAL_NOT_A_CANONICAL_FIELD,
                f"`{name}` is published by this Semantic View and is not a "
                "canonical field of this Project, so no measurement grain can "
                "state what it is reported against.",
                f"Declare `{name}` as a canonical field in Governance > Master "
                "Data, then declare the measurement grain that relates it to the "
                "dimensions this extract cuts by.",
                member=name,
            )
        resolved[name] = field_id
    return resolved


def _sanctioned_grains(
    conn,
    *,
    project_id: str,
    metrics: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Mapping[str, Any]],
    daily: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """One live grain version per metric, over the SAME dimension list. Refuse by name.

    The day is part of the cut and is passed as one of the dimensions: a grain
    that does not name the day does not sanction a daily file, and letting the
    day through unsanctioned would be the one axis of an MMM nobody had declared.
    """
    from core.analytics_alignment_read import sanction_breakdown  # noqa: PLC0415

    cut = [dict(daily)] + [dict(member) for member in dimensions]
    names = [member["name"] for member in metrics] + [member["name"] for member in cut]
    field_ids = _canonical_ids(conn, project_id=project_id, names=names)
    dimension_field_ids = [field_ids[member["name"]] for member in cut]

    sanctioned: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        verdict = sanction_breakdown(
            conn,
            project_id=project_id,
            metric_field_id=field_ids[metric["name"]],
            dimension_field_ids=dimension_field_ids,
        )
        if not verdict.get("sanctioned"):
            refusal = verdict.get("refusal") or {}
            # ITS WORDS, NOT A SECOND SET. `sanction_breakdown` already names the
            # three refusals and the gesture that repairs each; respelling them
            # here would give the product two sentences for one governance fact.
            raise MmmExportRefused(
                _text(refusal.get("code")) or "breakdown_not_sanctioned",
                f"{metric['name']}: {refusal.get('reason')}",
                _text(refusal.get("gesture")),
                metric=metric["name"],
                grain_versions=verdict.get("grain_versions") or [],
            )
        sliced = verdict.get("sliced_by") or {}
        sanctioned[metric["name"]] = {
            "measurement_grain_id": _text(sliced.get("measurement_grain_id")),
            # THE VERSION, NEVER THE ID ALONE. « a consumer pins a grain without a
            # version » is a clause of the amendment that created the object.
            "version_id": _text(sliced.get("version_id")),
            "measurement_grain_name": verdict.get("measurement_grain_name"),
        }
    return sanctioned


# ---------------------------------------------------------------------------
# Gate 4 -- one declared currency per monetary metric, or the file is refused.
# ---------------------------------------------------------------------------


def _qualified(plan: Mapping[str, Any]) -> str:
    """`dataset.relation`, composed exactly as `build_sql` composes it."""
    relation = _text(plan.get("relation"))
    if "." in relation:
        return relation
    dataset = _text(plan.get("dataset"))
    return f"{dataset}.{relation}" if dataset else relation


def _readable(part: str) -> bool:
    """The compiler's own identifier rule, asked through its public predicate.

    `names_a_readable_relation` documents itself as *"exactly the rule
    `build_sql` applies part by part"*, and a bare column name is one part. Asked
    rather than respelled: a second regex here is a second answer to one question.
    """
    from core.query_execution import names_a_readable_relation  # noqa: PLC0415

    return names_a_readable_relation(part)


def _distinct_currencies(
    *, plan: Mapping[str, Any], column: str, date_column: str,
    first: date, last: date,
) -> list[str]:
    """The currencies one amount column is denominated in, over the window.

    BOUNDED AND METADATA-CHEAP, the posture `relation_shape._breakdown_keys`
    already takes: one `DISTINCT` over one column of one relation, restricted to
    the window the file covers, capped. It is asked once per monetary metric and
    only because its answer DECIDES something -- whether the file may exist.
    """
    from core import warehouse  # noqa: PLC0415

    relation = _qualified(plan)
    if not all(_readable(part) for part in relation.split(".")):
        return []
    if not _readable(column) or not _readable(date_column):
        return []
    sql = (
        f"SELECT DISTINCT {column} AS c FROM {relation} "  # noqa: S608 -- identifiers checked
        f"WHERE {column} IS NOT NULL AND {date_column} >= ? AND {date_column} <= ? "
        "LIMIT 50"
    )
    params: list[Any] = [first.isoformat(), last.isoformat()]
    mode = warehouse._db_mode()
    if mode == "bigquery":
        rows = warehouse._query_bigquery(_positional(sql), params)
    elif mode == "duckdb":
        rows = warehouse._query_duckdb(sql, params)
    else:
        return []
    return sorted({_text(row.get("c")) for row in rows if _text(row.get("c"))})


def _positional(sql: str) -> str:
    from core.query_execution import _bind_positional  # noqa: PLC0415

    return _bind_positional(sql)


def _currencies(
    *,
    plan: Mapping[str, Any],
    metrics: Sequence[Mapping[str, Any]],
    daily: Mapping[str, Any],
    first: date,
    last: date,
) -> dict[str, dict[str, Any]]:
    """Per metric: the single currency it is denominated in, or a refusal.

    WHAT MAKES A METRIC MONETARY IS THE ONE AUTHORITY, and it is not a name: a
    published Concept version whose `value_type` is `money` -- the same predicate
    `money_provenance_columns.monetary_concept_names` applies to a Project. It is
    read here off the version this View PINS rather than off the Concept's current
    version, because the file belongs to this View version and a Concept
    re-published as money tomorrow must not retro-classify a file taken today.
    A metric that is not money carries no currency, and that is not a gap.
    """
    from core.currency_refusal import (  # noqa: PLC0415
        REFUSAL_CROSS_CURRENCY,
        REFUSAL_UNKNOWN_CURRENCY,
    )
    from core.money_provenance_columns import (  # noqa: PLC0415
        ZONE_MAPPED,
        native_currency_column,
    )

    present = [str(column) for column in plan.get("present_columns") or []]
    date_column = _text((plan.get("columns") or {}).get(daily["concept_id"]))
    declared = {
        member["name"] for member in metrics if member.get("value_type") == "money"
    }
    out: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        name = metric["name"]
        if name not in declared:
            out[name] = {
                "currency": NO_CURRENCY,
                "monetary": False,
                "reason": "This metric is not an amount, so it carries no currency.",
            }
            continue
        physical = _text((plan.get("columns") or {}).get(metric["concept_id"]))
        currency_column = native_currency_column(physical, present)
        if not currency_column:
            raise MmmExportRefused(
                REFUSAL_UNKNOWN_CURRENCY,
                f"`{name}` is an amount and the Datastream that answers this "
                "extract does not report which currency it is in, so what it "
                "counts is unknown.",
                "Bind this metric to a Datastream that reports its source "
                "currency, or declare the source currency of this one in Data > "
                "its Datastream, then extract again.",
                metric=name,
                zone=ZONE_MAPPED,
            )
        try:
            found = _distinct_currencies(
                plan=plan, column=currency_column,
                date_column=date_column, first=first, last=last,
            )
        except Exception as exc:  # noqa: BLE001
            # « I could not look » is not « there is nothing », and this gate
            # fails CLOSED either way -- but it must say which of the two it was.
            logger.warning("mmm_export: currency probe unreadable %s: %s", name, exc)
            raise MmmExportRefused(
                REFUSAL_WAREHOUSE_UNREADABLE,
                f"The currency `{name}` is reported in could not be read, so this "
                "extract cannot state what its amounts count.",
                "Try again; if it keeps failing, the Datastream's Runs tab "
                "carries the landing that cannot be read.",
                metric=name,
            ) from exc
        if not found:
            raise MmmExportRefused(
                REFUSAL_UNKNOWN_CURRENCY,
                f"No row of `{name}` in this window declares a currency, so its "
                "amounts count something nobody has named.",
                "Declare the source currency of this Datastream, re-run it, then "
                "extract again.",
                metric=name,
                currency_column=currency_column,
            )
        if len(found) > 1:
            raise MmmExportRefused(
                REFUSAL_CROSS_CURRENCY,
                f"`{name}` is reported in {_listed(found)} over this window, and "
                "one column of one file cannot hold two currencies without saying "
                "which rows are which.",
                "Extract one currency at a time, or convert this measure to the "
                "Project's reporting currency and extract the converted metric.",
                metric=name,
                currencies=found,
            )
        out[name] = {
            "currency": found[0],
            "monetary": True,
            # NAMED, so nobody reads a converted figure into a native one.
            "conversion": "none -- the amount is exactly what the source reported",
            "source_column": currency_column,
        }
    return out


# ---------------------------------------------------------------------------
# The read, and the melt into long form.
# ---------------------------------------------------------------------------


def _spec(
    *,
    metrics: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Mapping[str, Any]],
    daily: Mapping[str, Any],
    first: date,
    last: date,
) -> dict[str, Any]:
    """The Query Spec this extract is, in the shape the governed planner reads.

    The day is both a dimension (so the read groups by it) and the time member
    (so the read is bounded by the window) -- the same shape every governed
    execution of this repository builds.
    """
    return {
        "measures": [
            {"id": member["concept_id"], "version_id": member["concept_version_id"]}
            for member in metrics
        ],
        "dimensions": [
            {"id": member["concept_id"], "version_id": member["concept_version_id"]}
            for member in [daily, *dimensions]
        ],
        "filters": [],
        "sort": [],
        "grain": "day",
        "time": {
            "member_id": daily["concept_id"],
            "start": first.isoformat(),
            "end": last.isoformat(),
        },
    }


def _run(plan: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    from core import warehouse  # noqa: PLC0415
    from core.query_execution import build_sql  # noqa: PLC0415

    sql, params = build_sql(dict(plan), dict(spec), max_rows=MAX_SOURCE_ROWS)
    if warehouse._db_mode() == "bigquery":
        return warehouse._query_bigquery(_positional(sql), params)
    return warehouse._query_duckdb(sql, params)


def _melt(
    rows: Sequence[Mapping[str, Any]],
    *,
    plan: Mapping[str, Any],
    metrics: Sequence[Mapping[str, Any]],
    dimensions: Sequence[Mapping[str, Any]],
    daily: Mapping[str, Any],
    grains: Mapping[str, Mapping[str, Any]],
    currencies: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Wide rows -> one row per date x cut x metric, each carrying its own pins.

    A NULL measure is DROPPED and not written as an empty value. On a landing
    that reports measurements as rows, the aggregate of a metric absent from a
    day is NULL, and a `value` cell holding nothing is a day a model would read
    as a zero. What the drop leaves behind is not silence: the day reappears in
    the date gaps below, by name.
    """
    columns = plan.get("columns") or {}
    long_rows: list[dict[str, Any]] = []
    for row in rows:
        base = {"date": _text(row.get(_text(columns.get(daily["concept_id"]))))}
        for member in dimensions:
            base[member["name"]] = row.get(_text(columns.get(member["concept_id"])))
        for metric in metrics:
            value = row.get(_text(columns.get(metric["concept_id"])))
            if value is None:
                continue
            long_rows.append(
                {
                    **base,
                    "metric": metric["name"],
                    "value": value,
                    "currency": _text(
                        (currencies.get(metric["name"]) or {}).get("currency")
                    ),
                    "measurement_grain_version_id": _text(
                        (grains.get(metric["name"]) or {}).get("version_id")
                    ),
                }
            )
    return long_rows


def _date_gaps(
    long_rows: Sequence[Mapping[str, Any]],
    *,
    metrics: Sequence[Mapping[str, Any]],
    days: Sequence[str],
) -> dict[str, Any]:
    """Per metric, the days of the window that carry no row. Named, never silent."""
    seen: dict[str, set[str]] = {metric["name"]: set() for metric in metrics}
    for row in long_rows:
        bucket = seen.get(_text(row.get("metric")))
        if bucket is not None:
            bucket.add(_text(row.get("date")))
    per_metric: dict[str, Any] = {}
    total = 0
    for metric in metrics:
        missing = [day for day in days if day not in seen[metric["name"]]]
        total += len(missing)
        per_metric[metric["name"]] = {
            "count": len(missing),
            "dates": missing[:MAX_LISTED_GAPS],
            "listed": min(len(missing), MAX_LISTED_GAPS),
        }
    return {
        "total": total,
        "per_metric": per_metric,
        "message": (
            "Every day of the window carries a row for every metric."
            if total == 0
            else (
                f"{total} metric-days of this window carry no row. A week missing "
                "from an extract moves a mix model's coefficient, so they are "
                "listed here rather than left for the model to absorb."
            )
        ),
    }


# ---------------------------------------------------------------------------
# The one public composition.
# ---------------------------------------------------------------------------


def build_extract(
    conn,
    *,
    project_id: str,
    semantic_view_version_id: str,
    metrics: Sequence[str],
    dimensions: Sequence[str],
    start: Any,
    end: Any,
) -> dict[str, Any]:
    """The MMM extract of one published Semantic View version. Reads, writes nothing.

    Raises :class:`MmmExportRefused` at every gate. Returns
    ``{columns, rows, provenance}`` where each row is one date x cut x metric and
    carries the grain version that sanctioned it and the currency it counts.
    """
    from core.query_execution import resolve_physical_plan  # noqa: PLC0415
    from core.query_specs import canonical_hash  # noqa: PLC0415

    if not metrics:
        raise MmmExportRefused(
            REFUSAL_NO_MEASURE_ASKED,
            "An extract with no metric is a list of days.",
            "Name at least one metric this Semantic View publishes.",
        )
    first, last = _window(start, end)
    view = _load_view(
        conn, project_id=project_id, semantic_view_version_id=semantic_view_version_id
    )
    members = _load_members(
        conn, project_id=project_id, semantic_view_version_id=semantic_view_version_id
    )
    daily = _daily_member(members)
    measures = _pick(members, list(metrics), "metric")
    cut = [
        member
        for member in _pick(members, list(dimensions), "dimension")
        if member["concept_id"] != daily["concept_id"]
    ]

    grains = _sanctioned_grains(
        conn, project_id=project_id, metrics=measures, dimensions=cut, daily=daily
    )

    spec = _spec(metrics=measures, dimensions=cut, daily=daily, first=first, last=last)
    plan = resolve_physical_plan(
        conn,
        project_id=project_id,
        semantic_view_version_id=semantic_view_version_id,
        spec=spec,
    )
    if "unavailable_reason" in plan:
        # THE PLANNER'S OWN SENTENCE, CARRIED WHOLE. It already names the gesture
        # for each broken link -- an inactive binding, a Datastream that never
        # ran, a request crossing two sources, a breakdown the relation does not
        # publish. A second wording here would be a second answer.
        raise MmmExportRefused(
            REFUSAL_PLAN_UNAVAILABLE,
            _text(plan.get("unavailable_reason")),
            "Repair the link this names on the Semantic View or the Datastream, "
            "then extract again.",
            missing_link=_text(plan.get("missing_link")),
        )

    currencies = _currencies(
        plan=plan, metrics=measures, daily=daily, first=first, last=last
    )

    try:
        wide = _run(plan, spec)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mmm_export: warehouse_unreadable relation=%s: %s: %s",
            plan.get("relation"), type(exc).__name__, exc, exc_info=True,
        )
        raise MmmExportRefused(
            REFUSAL_WAREHOUSE_UNREADABLE,
            "The rows behind this extract could not be read, so no file was "
            "produced. This is not an empty extract.",
            "Try again; if it keeps failing, the Datastream's Runs tab carries "
            "the run whose landing cannot be read.",
            relation=_text(plan.get("relation")),
        ) from exc

    if len(wide) > MAX_SOURCE_ROWS:
        # `build_sql` asked for one row over the bound precisely so this can be
        # counted rather than trusted. A truncated file is a file a person will
        # treat as complete.
        raise MmmExportRefused(
            REFUSAL_TOO_MANY_ROWS,
            f"This window and this cut produce more than {MAX_SOURCE_ROWS} source "
            "rows, which is more than one extract carries. Nothing was truncated.",
            "Narrow the window, or drop a dimension from the cut, and take the "
            "rest as a second file.",
            maximum=MAX_SOURCE_ROWS,
        )

    long_rows = _melt(
        wide, plan=plan, metrics=measures, dimensions=cut, daily=daily,
        grains=grains, currencies=currencies,
    )
    days = _days(first, last)
    columns = (
        ["date"]
        + [member["name"] for member in cut]
        + ["metric", "value", "currency", "measurement_grain_version_id"]
    )
    return {
        "columns": columns,
        "rows": long_rows,
        "provenance": {
            "extract": "mmm",
            "shape": "long -- one row per date x declared dimension x metric",
            "semantic_view_id": view["semantic_view_id"],
            "semantic_view_version_id": view["semantic_view_version_id"],
            "semantic_view_name": view["name"],
            "semantic_view_version_number": view["version_number"],
            "semantic_view_content_hash": view["content_hash"],
            "request_hash": canonical_hash(spec),
            "members": [
                {
                    "role": member["role"],
                    "name": member["name"],
                    "concept_id": member["concept_id"],
                    "concept_version_id": member["concept_version_id"],
                    "value_type": member["value_type"],
                    "unit": member["unit"],
                }
                for member in [daily, *cut, *measures]
            ],
            "measurement_grains": grains,
            "currencies": currencies,
            "datastream_id": _text(plan.get("datastream_id")),
            "mapping_version_id": _text(plan.get("mapping_version_id")),
            "relation": _text(plan.get("relation")),
            "dataset": _text(plan.get("dataset")),
            "output_id": _text(plan.get("output_id")),
            "output_version_id": _text(plan.get("output_version_id")),
            "pull_id": _text(plan.get("pull_id")),
            "publication_log_id": _text(plan.get("publication_log_id")),
            "chosen_by": plan.get("chosen_by"),
            "window": {
                "start": first.isoformat(),
                "end": last.isoformat(),
                "days": len(days),
            },
            "date_gaps": _date_gaps(long_rows, metrics=measures, days=days),
            "source_row_count": len(wide),
            "row_count": len(long_rows),
            "truncated": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "writes": "none -- this extract is a read and stores nothing",
        },
    }


def extract_csv(payload: Mapping[str, Any]) -> bytes:
    """The table alone, and nothing the read did not return.

    NO COMMENT BLOCK AND NO PROVENANCE HEADER LINES. A `#` preamble is a dialect
    every reading tool has to be told about, and a file half of whose lines are
    not the table is a file somebody will load wrong once. The provenance is the
    JSON representation of the very same read -- the companion the ratified
    section names -- and it is produced by the same call.
    """
    columns = [str(column) for column in payload.get("columns") or []]
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in payload.get("rows") or []:
        writer.writerow(
            ["" if row.get(column) is None else row.get(column) for column in columns]
        )
    return out.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# THE SEAM 62.2 READS, and it is deliberately four names and no fifth.
#
# `capabilities/analytics-alignment.md` §4 says planned-versus-actual "must land
# as a second reader over this seam, not as a second export module with a second
# file shape". What is genuinely shared is the *envelope* of an analytical
# extract -- the window and its bounds, the refusal type, the row cap and the CSV
# writer -- not the reading: one resolves a Semantic View, the other reads the
# consolidated variance mart, and pretending those are one function would be a
# third answer to two different questions.
#
# The aliases below exist so the second reader IMPORTS these rather than
# respelling them. `_window` and `_days` keep their private names because
# `build_extract` above calls them by those names on every line; renaming them
# would be churn in the one path this module is measured on.
# ---------------------------------------------------------------------------

#: The refusal of an analytical extract. `MmmExportRefused` is its first name and
#: stays it -- same class object, so `except MmmExportRefused` still catches what
#: a planned-versus-actual read raises.
ExportRefused = MmmExportRefused

#: Two calendar days, in order, within `MAX_WINDOW_DAYS`. Raises `ExportRefused`.
parse_window = _window

#: Every ISO day of an inclusive window, so a hole can be named by date.
window_days = _days


__all__ = [
    "MAX_LISTED_GAPS",
    "MAX_SOURCE_ROWS",
    "MAX_WINDOW_DAYS",
    "NO_CURRENCY",
    "REFUSAL_TOO_MANY_ROWS",
    "REFUSAL_WAREHOUSE_UNREADABLE",
    "REFUSAL_WINDOW_MALFORMED",
    "REFUSAL_WINDOW_TOO_WIDE",
    "ExportRefused",
    "MmmExportRefused",
    "build_extract",
    "extract_csv",
    "parse_window",
    "window_days",
]
