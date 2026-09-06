"""Story 62.2 -- the planned-versus-actual extract: the second reader of the seam.

WHAT THIS IS. It is a READ, and it is the SECOND reader of `core.mmm_export`, not
a second export module. `docs/product-architecture/capabilities/analytics-alignment.md`
§4 wrote the rule before either story's code -- *"Planned-versus-actual differs
only in what it reads [...] and must land as a second reader over this seam, not
as a second export module with a second file shape"* -- and the shared envelope
is imported here rather than respelled: the refusal type (`ExportRefused`), the
window parser and its bounds (`parse_window`, `window_days`, `MAX_WINDOW_DAYS`),
the row cap (`MAX_SOURCE_ROWS`) and the CSV writer (`extract_csv`). What differs
is the reading, and only that.

WHAT IT READS, AND WHY IT RE-DERIVES NOTHING. The consolidated variance of epic
61 is `dbt/models/marts/plan_vs_actual_daily.sql`: one row per
`(project_id, plan_id, plan_version_id, line_key, day)`, the FULL OUTER JOIN of
the active version's daily allocation and the ventilated observed spend. That
mart is the ONE authority on the comparison -- it applies
`app.plan_line_mappings.split_weight` under the `SUM = 1.0` invariant the store
enforces, it decides the money gap once (`money_gap_code`, ordered by severity),
and it withholds an amount rather than understating it. This module reads it. It
adds no second ventilation, no second gap ladder and no second currency rule:
two answers in the product for one figure is the fault
`capabilities/placement-mapping.md` was written to close.

THE FOUR GATES, IN THE SAME ORDER AS THE MMM EXTRACT'S.

  1. the plan version is PUBLISHED and active. Not an analogy -- a real state:
     `app.media_plan_versions.status` is `candidate | published` (migration 040)
     and only `publish_version` materialises `plan_allocation_daily`. A budget
     nobody has published is a draft, and a file taken from one outlives it.
  2. the placement mapping is CONFIRMED. The gate proper to this extract, and
     the counterpart of the sanctioned grain: it is what makes the two sides
     comparable at all. No active match anywhere -> every line reads as never
     delivered, and a file of zeroes accuses a campaign that ran. Any `orphaned`
     match -> a `line_key` that vanished from the active version keeps its match
     and receives no ventilation, so the observed side is understated with
     nothing on the file saying so.
  3. the mart resolves. `plan_vs_actual_daily` builds EMPTY behind
     `toorow_absent_sources` when the plan mirror is not in this warehouse
     (AI-314), so zero rows for a plan that HAS a published version and active
     matches is refused, never served: an empty CSV is the file a person treats
     as complete. Gate 3 is after gate 2 for the reason it is after gate 2 in
     `mmm_export` -- a mapping nobody confirmed is a fact about the declaration
     and stays true whatever the warehouse answers.
  4. exactly ONE declared currency over the window, fails closed on zero and on
     two, in `core.currency_refusal`'s own two words. The mart already labels
     each amount with the currency that PRODUCED it, so this gate reads its
     labels instead of minting a third opinion.

THE TWO PINS ON EVERY ROW. A variance is produced by TWO declarations, so the
tail of the long shape carries two: `plan_version_id`, the media-plan version
whose allocation stated the budget, and `placement_mapping_fingerprint`, the
SHA-256 of the plan's ACTIVE match set. There is no placement-mapping VERSION to
pin -- measured 2026-09-01, `app.plan_line_mappings` (migrations 041, 246) has no
version column and no publication, and `set_line_mappings` replaces a line's
whole set in place; `mapping_version_id` everywhere else in this repository names
`app.datastream_mapping_versions`, a different object. So the extract states what
it can measure, derives it at read, stores it nowhere, and calls it a fingerprint
rather than dressing it as a version.

A PLANNED DAY WITHOUT AN ACTUAL IS A VARIANCE, NOT A HOLE. It is the date-gap
rule applied to a comparison, and the mart already measured the distinction:
`actual_amount` is NULL for both "did not spend" and "could not be stated", and
`actual_withheld` separates them. Absorbing an under-delivery into a gap count
would hide the very figure this file exists for; calling a withheld amount a
variance would accuse a campaign of not spending when all that was missing was an
exchange rate. Four counts, never merged -- and only the holes are the total.

IT WRITES NOTHING. No table, no dataset, no bucket, no row of its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

# THE SEAM. Imported, never respelled -- the envelope of an analytical extract
# that §4 says the two stories share.
from core.mmm_export import (
    MAX_LISTED_GAPS,
    MAX_SOURCE_ROWS,
    NO_CURRENCY,
    REFUSAL_TOO_MANY_ROWS,
    REFUSAL_WAREHOUSE_UNREADABLE,
    ExportRefused,
    extract_csv,
    parse_window,
    window_days,
)

logger = logging.getLogger(__name__)

#: The two values the `metric` column takes. The variance is deliberately NOT a
#: third: it is `planned_spend - actual_spend` at the same (date, line) key, so
#: it is derivable by whoever reads the file, and emitting it would put a
#: difference in the same `value` column as the two amounts it is made of --
#: any consumer grouping by date alone would then count the money twice.
METRIC_PLANNED = "planned_spend"
METRIC_ACTUAL = "actual_spend"

#: The one relation this extract reads. A hard-coded constant, never composed
#: from caller input -- the discipline `warehouse._build_plan_pacing_query`
#: already states for the three pacing marts beside it.
_RELATION = "plan_vs_actual_daily"

#: The columns of that mart this read selects, by name. `SELECT *` would make the
#: file's shape depend on a rebuild of the mart, which is exactly what a stable
#: extract may not do.
_COLUMNS = (
    "plan_version_id",
    "line_key",
    "label",
    "channel",
    "day",
    "is_plan_only",
    "sort_order",
    "plan_currency",
    "actual_currency",
    "reporting_currency",
    "money_policy_version_id",
    "money_gap_code",
    "budget",
    "allocated_amount",
    "actual_amount",
    "actual_withheld",
    "actual_pull_id",
)


# ---------------------------------------------------------------------------
# Refusals proper to this extract. Every one carries the gesture that repairs it.
# ---------------------------------------------------------------------------

REFUSAL_NO_PLAN_ASKED = "no_media_plan_asked"
REFUSAL_PLAN_NOT_FOUND = "media_plan_not_found"
REFUSAL_PLAN_ARCHIVED = "media_plan_archived"
REFUSAL_PLAN_NOT_PUBLISHED = "media_plan_version_not_published"
REFUSAL_MAPPING_NOT_CONFIRMED = "placement_mapping_not_confirmed"
REFUSAL_MAPPING_ORPHANED = "placement_mapping_orphaned"
REFUSAL_MART_UNAVAILABLE = "plan_vs_actual_mart_unavailable"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _listed(names: Sequence[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _iso_day(value: Any) -> str:
    """The mart's `day` as an ISO calendar day, whatever the driver returned.

    DuckDB hands back a `datetime.date`, BigQuery a `datetime.date`, and the dev
    seed path writes ISO strings that survive as `VARCHAR` -- the same three
    shapes `mediaplan_mapping._parse_iso_day` already normalises.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)[:10]


def _amount(value: Any) -> Any:
    """A monetary cell, as the mart published it, or None.

    NOT re-rounded and NOT re-scaled here. `plan_vs_actual_daily` normalises every
    quantity to integer micros per source row and derives its display columns from
    them ONCE (`fee_tax_from_micros`); a second arithmetic step in the export
    would be a second authority on the cent.
    """
    return None if value is None else value


# ---------------------------------------------------------------------------
# Gate 1 -- the plan, and its published active version.
# ---------------------------------------------------------------------------

_PLAN = """
    SELECT p.id, p.name, p.currency, p.archived_at,
           v.id, v.version_number, v.status, v.is_active
      FROM app.media_plans p
      LEFT JOIN app.media_plan_versions v
             ON v.plan_id = p.id AND v.is_active
     WHERE p.id = %(plan_id)s
       AND p.project_id = %(project_id)s
"""


def _load_plan(conn, *, project_id: str, plan_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_PLAN, {"plan_id": plan_id, "project_id": project_id})
        row = cur.fetchone()
    if row is None:
        raise ExportRefused(
            REFUSAL_PLAN_NOT_FOUND,
            "This Project has no media plan under that address.",
            "Open Governance > Media plans, choose the plan to extract, and take "
            "its id.",
        )
    if row[3] is not None:
        raise ExportRefused(
            REFUSAL_PLAN_ARCHIVED,
            f"The media plan `{_text(row[1])}` is archived, so what it planned is "
            "no longer a budget anybody is pacing against.",
            "Extract from a plan that is not archived, or restore this one in "
            "Governance > Media plans.",
            plan_name=_text(row[1]),
        )
    version_id = _text(row[4])
    status = _text(row[6])
    if not version_id:
        # A PUBLISHED VERSION IS THE ONLY ONE THAT ALLOCATED ANYTHING.
        # `publish_version` is what materialises `app.plan_allocation_daily`, and
        # `plan_vs_actual_daily.sql` reads the ACTIVE version alone. With no
        # active version there is no planned side at all, and a file whose
        # planned column is empty everywhere is not a variance, it is a list of
        # observed spend wearing a plan's name.
        raise ExportRefused(
            REFUSAL_PLAN_NOT_PUBLISHED,
            f"The media plan `{_text(row[1])}` has no published version, so it has "
            "allocated no budget to compare anything against.",
            "Publish a version of this plan in Governance > Media plans, then "
            "extract again.",
            plan_name=_text(row[1]),
        )
    if status != "published":
        raise ExportRefused(
            REFUSAL_PLAN_NOT_PUBLISHED,
            f"The active version of `{_text(row[1])}` is {status}, not published, "
            "so the budget it states has not been agreed yet.",
            "Publish this version in Governance > Media plans. A candidate can "
            "still change under a file somebody has already reported on.",
            plan_name=_text(row[1]),
            media_plan_version_status=status,
        )
    return {
        "plan_id": _text(row[0]),
        "plan_name": _text(row[1]),
        "plan_currency": _text(row[2]),
        "plan_version_id": version_id,
        "version_number": row[5],
        "status": status,
    }


# ---------------------------------------------------------------------------
# Gate 2 -- the placement mapping is confirmed, and its fingerprint.
# ---------------------------------------------------------------------------

_MAPPINGS = """
    SELECT line_key, connector, campaign_ref, split_weight, status,
           match_method, match_score, updated_at
      FROM app.plan_line_mappings
     WHERE plan_id = %(plan_id)s
     ORDER BY line_key, connector, campaign_ref
"""


def _confirmed_mapping(conn, *, plan: Mapping[str, Any]) -> dict[str, Any]:
    """The active match set, refused when it is absent or orphaned, and its pin.

    THE TWO REFUSALS ARE THE SAME FAULT SEEN TWICE: an observed side understated
    with nothing on the file saying so. `plan_vs_actual_daily.sql` ventilates on
    `status = 'active'` alone -- the one column
    `plan_matching_states.py` also calls the decider -- so a plan with no active
    match produces a file whose `actual_spend` rows do not exist, and an orphaned
    match produces one where a campaign's spend silently reaches nobody.
    """
    from core.query_specs import canonical_hash  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(_MAPPINGS, {"plan_id": plan["plan_id"]})
        rows = cur.fetchall()

    active = [row for row in rows if _text(row[4]) == "active"]
    orphaned = sorted({_text(row[0]) for row in rows if _text(row[4]) == "orphaned"})
    if orphaned:
        raise ExportRefused(
            REFUSAL_MAPPING_ORPHANED,
            "The placement mapping of this plan is not confirmed: "
            + _listed(orphaned)
            + " no longer exist in the published version, and the campaigns still "
            "attached to them are ventilated nowhere. The observed side of the "
            "file would be short by their spend without saying so.",
            "Open Data > the Datastream Workbench > Placements and re-attach each "
            "of these matches to a line of the published version, or remove it.",
            plan_id=plan["plan_id"],
            orphaned_line_keys=orphaned,
        )
    if not active:
        raise ExportRefused(
            REFUSAL_MAPPING_NOT_CONFIRMED,
            "No campaign is attached to any line of this plan, so every line would "
            "read as having delivered nothing. A file of zeroes accuses a campaign "
            "that ran.",
            "Open Data > the Datastream Workbench > Placements and attach at least "
            "one campaign to a line of this plan, then extract again.",
            plan_id=plan["plan_id"],
        )

    # THE FINGERPRINT IS DERIVED AND STORED NOWHERE -- « une valeur derivable ne
    # se stocke pas ». It is the content of the set that ventilated these rows,
    # hashed by the one hasher this repository already uses for a request.
    material = [
        [
            _text(row[0]),
            _text(row[1]),
            _text(row[2]),
            _text(row[3]),
            _text(row[5]),
            _text(row[6]),
        ]
        for row in active
    ]
    methods: dict[str, int] = {}
    for row in active:
        # NULL IS AN ABSENCE AND NEVER `manual` -- migration 246's own rule, and
        # `capabilities/placement-mapping.md` refuses the substitution by name.
        key = _text(row[5]) or "not_recorded"
        methods[key] = methods.get(key, 0) + 1
    changed = [row[7] for row in active if row[7] is not None]
    return {
        "fingerprint": canonical_hash(material),
        "active_match_count": len(active),
        "orphaned_match_count": 0,
        "match_methods": dict(sorted(methods.items())),
        "last_changed_at": max(changed).isoformat() if changed else "",
    }


# ---------------------------------------------------------------------------
# Gate 3 -- the mart resolves, and it is read by name.
# ---------------------------------------------------------------------------


def _read_mart(
    *, project_id: str, plan_id: str, first: date, last: date
) -> list[dict[str, Any]]:
    """`plan_vs_actual_daily` for one plan over one window. Bound parameters only.

    One row over the cap is asked for deliberately, so the refusal below can be
    COUNTED rather than trusted -- the posture `mmm_export` takes with
    `MAX_SOURCE_ROWS`.
    """
    from core import warehouse  # noqa: PLC0415

    mode = warehouse._db_mode()
    projection = ", ".join(_COLUMNS)
    params: list[Any] = [project_id, plan_id, first.isoformat(), last.isoformat()]
    if mode == "duckdb":
        prefix = warehouse._duckdb_mart_prefix(project_id)
        holes = ("?", "?", "?", "?")
    elif mode == "bigquery":
        prefix = ""
        holes = ("@p0", "@p1", "@p2", "@p3")
    else:
        raise ValueError(f"Unknown TOOROW_DB_MODE: {mode!r}")
    sql = (
        f"SELECT {projection} FROM {prefix}{_RELATION} "  # noqa: S608 -- constants
        f"WHERE project_id = {holes[0]} AND plan_id = {holes[1]} "
        f"AND day >= {holes[2]} AND day <= {holes[3]} "
        f"ORDER BY day, sort_order, line_key "
        f"LIMIT {MAX_SOURCE_ROWS + 1}"
    )
    if mode == "bigquery":
        return warehouse._query_bigquery(sql, params)
    return warehouse._query_duckdb(sql, params)


def _mart_rows(
    *, project_id: str, plan: Mapping[str, Any], first: date, last: date
) -> list[dict[str, Any]]:
    try:
        rows = _read_mart(
            project_id=project_id, plan_id=plan["plan_id"], first=first, last=last
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "planned_actual_export: mart unreadable plan=%s: %s: %s",
            plan["plan_id"], type(exc).__name__, exc, exc_info=True,
        )
        raise ExportRefused(
            REFUSAL_WAREHOUSE_UNREADABLE,
            "The consolidated variance of this plan could not be read, so no file "
            "was produced. This is not an empty extract.",
            "Try again; if it keeps failing, the plan's warehouse mart has not "
            "been rebuilt since the plan was published.",
            relation=_RELATION,
        ) from exc
    if len(rows) > MAX_SOURCE_ROWS:
        raise ExportRefused(
            REFUSAL_TOO_MANY_ROWS,
            f"This plan and this window produce more than {MAX_SOURCE_ROWS} source "
            "rows, which is more than one extract carries. Nothing was truncated.",
            "Narrow the window and take the rest as a second file.",
            maximum=MAX_SOURCE_ROWS,
        )
    if not rows:
        # AN EMPTY FILE IS NOT AN HONEST EMPTY HERE. Gates 1 and 2 already proved
        # this plan has a published version and confirmed matches, so a mart with
        # nothing in it means the mart does not carry this plan -- the model
        # builds EMPTY behind `toorow_absent_sources` when `mirror_sync` has not
        # landed the plan relations in this warehouse (AI-314). Serving a
        # zero-row CSV would hand somebody a file they will treat as complete.
        raise ExportRefused(
            REFUSAL_MART_UNAVAILABLE,
            f"`{plan['plan_name']}` has a published version and confirmed matches, "
            "and the warehouse holds no consolidated variance for it over this "
            "window, so there is nothing to extract rather than nothing to report.",
            "Rebuild this Project's warehouse so the published plan reaches it, "
            "then extract again. If the window falls entirely outside the plan's "
            "flight dates, ask for the window the plan covers.",
            relation=_RELATION,
            window={"start": first.isoformat(), "end": last.isoformat()},
        )
    return rows


# ---------------------------------------------------------------------------
# Gate 4 -- exactly one declared currency, in the mart's own labels.
# ---------------------------------------------------------------------------


def _currency(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One currency for the whole file, or a named refusal. Fails closed both ways.

    THE MART'S LABELS, NOT A THIRD OPINION. `plan_currency` names what produced
    `allocated_amount`, `actual_currency` names what produced `actual_amount`, and
    `money_gap_code = 'plan_currency_mismatch'` is the mart's own word for two
    currencies that cannot be composed. Reading them is the gate; recomputing them
    would be the second authority `placement-mapping.md` forbids.
    """
    from core.currency_refusal import (  # noqa: PLC0415
        REFUSAL_CROSS_CURRENCY,
        REFUSAL_UNKNOWN_CURRENCY,
    )

    planned = sorted({_text(row.get("plan_currency")) for row in rows} - {""})
    actual = sorted({_text(row.get("actual_currency")) for row in rows} - {""})
    codes = {_text(row.get("money_gap_code")) for row in rows} - {""}

    if "plan_currency_mismatch" in codes:
        raise ExportRefused(
            REFUSAL_CROSS_CURRENCY,
            "This plan states its budget in one currency and its spend was "
            "converted into another, so a planned-versus-actual figure taken from "
            "the two would be a wrong number that looks right.",
            "Set this plan's currency to the Project's reporting currency, or "
            "extract the two sides as separate files.",
            plan_currencies=planned,
            actual_currencies=actual,
        )
    both = sorted(set(planned) | set(actual))
    if len(both) > 1:
        raise ExportRefused(
            REFUSAL_CROSS_CURRENCY,
            f"This window carries amounts in {_listed(both)}, and one `value` "
            "column of one file cannot hold two currencies without saying which "
            "rows are which.",
            "Extract one currency at a time.",
            plan_currencies=planned,
            actual_currencies=actual,
        )
    if not both:
        raise ExportRefused(
            REFUSAL_UNKNOWN_CURRENCY,
            "No row of this window names the currency its amounts are in, so what "
            "the planned and observed figures count is unknown.",
            "Declare this plan's currency in Governance > Media plans and confirm "
            "the Project's Money Policy, then extract again.",
        )
    return {
        "currency": both[0],
        "plan_currency": planned[0] if planned else NO_CURRENCY,
        "actual_currency": actual[0] if actual else NO_CURRENCY,
        # NAMED, so nobody reads the observed side as native. The mart's observed
        # amount is `fx_convert_at_read('cost')` -- already converted into
        # `dim_project.canonical_currency` -- which is why gate 4 above insists
        # the two labels agree before either is printed.
        "conversion": "observed spend converted once at read, into this currency",
        "money_gap_codes": sorted(codes),
    }


# ---------------------------------------------------------------------------
# The melt into the long shape, and the four counts that are never merged.
# ---------------------------------------------------------------------------


def _melt(
    rows: Sequence[Mapping[str, Any]],
    *,
    plan: Mapping[str, Any],
    fingerprint: str,
    currency: str,
) -> list[dict[str, Any]]:
    """Mart rows -> one row per date x plan line x metric, each carrying both pins.

    A NULL amount is DROPPED and never written as an empty cell, the rule
    `mmm_export._melt` states: a `value` holding nothing is a figure a model or a
    spreadsheet reads as a zero. What the drop leaves behind is not silence -- the
    day reappears in the coverage block below, in the one of four counts that
    describes it.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        base = {
            "date": _iso_day(row.get("day")),
            "plan_line_key": _text(row.get("line_key")),
            "plan_line_label": _text(row.get("label")),
            "channel": _text(row.get("channel")),
        }
        pins = {
            "currency": currency,
            "plan_version_id": _text(row.get("plan_version_id"))
            or _text(plan.get("plan_version_id")),
            "placement_mapping_fingerprint": fingerprint,
        }
        planned = _amount(row.get("allocated_amount"))
        if planned is not None:
            out.append({**base, "metric": METRIC_PLANNED, "value": planned, **pins})
        actual = _amount(row.get("actual_amount"))
        if actual is not None:
            out.append({**base, "metric": METRIC_ACTUAL, "value": actual, **pins})
    return out


def _entry(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = {
        "date": _iso_day(row.get("day")),
        "plan_line_key": _text(row.get("line_key")),
    }
    code = _text(row.get("money_gap_code"))
    if code:
        entry["money_gap_code"] = code
    return entry


def _coverage(
    rows: Sequence[Mapping[str, Any]], *, days: Sequence[str]
) -> dict[str, Any]:
    """The four counts, and only the last of them is a hole.

    A PLANNED DAY WITHOUT AN ACTUAL IS A VARIANCE. The mart measured the
    distinction the file needs: `actual_amount` is NULL both for a line that did
    not spend and for a line whose money could not be stated, and
    `actual_withheld` is the column that separates them (« `actual_pull_id` is
    MAX(pull_id) of the contributing fact rows and survives a NULL value, so its
    presence is exactly the evidence that spend WAS there »). Merging the two
    would either hide an under-delivery -- the figure this file exists for -- or
    accuse a campaign of not spending when all that was missing was a rate.

    A `is_plan_only` line is neither: TV and OOH have no actuals source, so there
    is no honest spend to pace, and `placement-mapping.md` has downstream pacing
    skip them rather than read them at 0%.
    """
    variance: list[dict[str, Any]] = []
    unplanned: list[dict[str, Any]] = []
    plan_only: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        seen.add(_iso_day(row.get("day")))
        planned = row.get("allocated_amount") is not None
        actual = row.get("actual_amount") is not None
        if bool(row.get("actual_withheld")):
            withheld.append(_entry(row))
            continue
        if planned and not actual:
            if bool(row.get("is_plan_only")):
                plan_only.append(_entry(row))
            else:
                variance.append(_entry(row))
        elif actual and not planned:
            unplanned.append(_entry(row))

    absent = [{"date": day} for day in days if day not in seen]
    holes = len(withheld) + len(absent)

    def block(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(entries),
            "entries": entries[:MAX_LISTED_GAPS],
            "listed": min(len(entries), MAX_LISTED_GAPS),
        }

    return {
        "variance_days": block(variance),
        "unplanned_days": block(unplanned),
        "plan_only_days": block(plan_only),
        "withheld_days": block(withheld),
        "days_with_no_row": block(absent),
        "total_holes": holes,
        "message": (
            "Every day of the window carries a row, and every amount it holds "
            "could be stated."
            if holes == 0
            else (
                f"{holes} days of this window state no amount at all -- "
                f"{len(withheld)} whose money the warehouse refused to state, and "
                f"{len(absent)} carrying no row on either side. A planned day with "
                "no observed spend is NOT counted here: it is an under-delivery, "
                "and it is reported as `variance_days`."
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
    plan_id: str,
    start: Any,
    end: Any,
) -> dict[str, Any]:
    """The planned-versus-actual extract of one published media plan. Writes nothing.

    Raises :class:`core.mmm_export.ExportRefused` at every gate. Returns
    ``{columns, rows, provenance}``, where each row is one date x plan line x
    metric and carries the plan version and the placement-mapping fingerprint
    that produced it.
    """
    if not _text(plan_id):
        raise ExportRefused(
            REFUSAL_NO_PLAN_ASKED,
            "A planned-versus-actual extract compares one media plan, and none was "
            "named.",
            "Name the media plan to extract, from Governance > Media plans.",
        )
    first, last = parse_window(start, end)
    plan = _load_plan(conn, project_id=project_id, plan_id=plan_id)
    mapping = _confirmed_mapping(conn, plan=plan)
    rows = _mart_rows(project_id=project_id, plan=plan, first=first, last=last)
    currency = _currency(rows)

    long_rows = _melt(
        rows,
        plan=plan,
        fingerprint=mapping["fingerprint"],
        currency=currency["currency"],
    )
    days = window_days(first, last)
    columns = [
        "date",
        "plan_line_key",
        "plan_line_label",
        "channel",
        "metric",
        "value",
        "currency",
        "plan_version_id",
        "placement_mapping_fingerprint",
    ]
    return {
        "columns": columns,
        "rows": long_rows,
        "provenance": {
            "extract": "planned_vs_actual",
            "shape": "long -- one row per date x plan line x metric",
            "metrics": [METRIC_PLANNED, METRIC_ACTUAL],
            "variance": (
                f"derived by the reader: {METRIC_PLANNED} - {METRIC_ACTUAL} at the "
                "same date and plan line. It is not a third row, so no sum of "
                "`value` counts the same money twice"
            ),
            "media_plan_id": plan["plan_id"],
            "media_plan_name": plan["plan_name"],
            "media_plan_version_id": plan["plan_version_id"],
            "media_plan_version_number": plan["version_number"],
            "media_plan_version_status": plan["status"],
            "placement_mapping": mapping,
            "currency": currency,
            "relation": _RELATION,
            "ventilation": (
                "app.plan_line_mappings.split_weight, under SUM = 1.0 per "
                "(plan, connector, campaign) -- applied by the mart, not here"
            ),
            "actual_pull_ids": sorted(
                {_text(row.get("actual_pull_id")) for row in rows} - {""}
            )[:MAX_LISTED_GAPS],
            "window": {
                "start": first.isoformat(),
                "end": last.isoformat(),
                "days": len(days),
            },
            "date_coverage": _coverage(rows, days=days),
            "source_row_count": len(rows),
            "row_count": len(long_rows),
            "truncated": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "writes": "none -- this extract is a read and stores nothing",
        },
    }


__all__ = [
    "METRIC_ACTUAL",
    "METRIC_PLANNED",
    "REFUSAL_MAPPING_NOT_CONFIRMED",
    "REFUSAL_MAPPING_ORPHANED",
    "REFUSAL_MART_UNAVAILABLE",
    "REFUSAL_NO_PLAN_ASKED",
    "REFUSAL_PLAN_ARCHIVED",
    "REFUSAL_PLAN_NOT_FOUND",
    "REFUSAL_PLAN_NOT_PUBLISHED",
    "ExportRefused",
    "build_extract",
    "extract_csv",
]
