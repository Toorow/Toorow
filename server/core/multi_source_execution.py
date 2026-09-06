"""Story 66.5 -- executing a governed cross: aggregate, THEN merge.

THE ONE RULE THIS MODULE IS BUILT AROUND. Each fact source is filtered and
aggregated at the requested governed grain FIRST, and only the sub-results merge.
Joining two fact relations row to row and summing afterwards is the defect
66.3 measures and names: three spend rows against two conversion rows on one key
is six rows, and every measure of both sources is counted six times. Aggregating
first makes that arithmetically impossible rather than merely discouraged.

    m0 = SELECT keys, SUM(spend)       FROM spend       GROUP BY keys
    m1 = SELECT keys, SUM(conversions) FROM conversions GROUP BY keys
    merge m0 and m1 ON keys

WHAT A RATIO IS, AND WHAT IT IS NEVER. `SUM(revenue) / SUM(spend)`, computed
AFTER the merge from its two governed components. Never the sum of row ratios,
never their average -- the same rule the story 53.2 amendment states for rollups,
and the reason a ratio is declared as a pair of components rather than as a
number a source could hand over.

THE INCLUSION POLICY IS THE JOIN. `matched_only` is an inner join,
`preserve_primary` a left join from the plan's primary source, `preserve_all` a
full outer join. It is read off the frozen plan and never defaulted here: 66.4
refuses a plan that did not state one, precisely so this module never has to
choose.

NULL IS NOT ZERO. A key present on one side and absent on the other leaves the
other side's measures NULL. Only a measure contract that declares zero semantics
may render it as `0`, and no such contract exists yet -- so nothing here writes
a zero it did not measure.

IDENTIFIERS COME FROM THE PLAN, WHICH GOT THEM FROM A PUBLISHED MAPPING. The
allowlist guard is `query_execution._safe_identifier`, imported and not retyped.
No string from a request reaches the warehouse as a name.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from core.query_execution import (
    UNREADABLE_LANDING_MESSAGE,
    ExecutionUnavailable,
    _safe_identifier,
    names_a_readable_relation,
)

logger = logging.getLogger(__name__)

#: Aggregations a merged plan can compile. `average` is absent on purpose: an
#: average of averages is not an average, and recomputing it needs the component
#: counts a mapping does not publish today. It is an explicit refusal rather than
#: a silently wrong number.
COMPILABLE_AGGREGATIONS = frozenset({"sum", "min", "max", "count"})
MAX_MULTI_SOURCE_ROWS_BYTES = 1_500_000
COMPARISON_PERIOD_FIELD = "k_comparison_period"

_JOIN_FOR_POLICY = {
    "matched_only": "INNER JOIN",
    "preserve_primary": "LEFT JOIN",
    "preserve_all": "FULL OUTER JOIN",
}

_OPERATORS = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


class ExecutionRefused(ValueError):
    """The plan cannot be compiled to SQL, and the reason says which member."""

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _alias(prefix: str, canonical_field_id: str) -> str:
    """A stable, safe column alias derived from a canonical field id.

    The id is `mdm_<ULID>`; the alias keeps it whole so a caller can map a column
    back to the governed field without a side table, and `_safe_identifier`
    still gets the final say on what may be written.
    """
    return _safe_identifier(f"{prefix}_{canonical_field_id}")


def key_field_ids(plan: Mapping[str, Any]) -> list[str]:
    """The conformed canonical fields every member is grouped by, in a stable order."""
    seen: list[str] = []
    for edge in plan.get("edges") or []:
        for component in edge.get("components") or []:
            field_id = str(component.get("canonical_field_id") or "")
            if field_id and field_id not in seen:
                seen.append(field_id)
    return seen


def output_dimension_field_ids(plan: Mapping[str, Any]) -> list[str]:
    """The conformed dimensions the Result exposes, not every internal join key."""
    selected = [
        str(entry.get("canonical_field_id") or "")
        for entry in plan.get("dimensions") or []
        if entry.get("canonical_field_id")
    ]
    # Stored plans created before explicit dimension execution carried an empty
    # list and exposed every conformed key. Preserve that byte/behaviour contract;
    # a non-empty list is the new exact projection.
    if not selected:
        return key_field_ids(plan)
    grain = str(plan.get("grain_canonical_field_id") or "")
    if grain and grain not in selected:
        selected.insert(0, grain)
    return selected


def _measure_name(measure: Mapping[str, Any]) -> str:
    return _safe_identifier(
        str(measure.get("result_field") or _alias("m", str(measure["canonical_field_id"])))
    )


def _member_key_field_ids(plan: Mapping[str, Any], member: Mapping[str, Any]) -> list[str]:
    carried = _member_key_columns(plan, member)
    return [field_id for field_id in key_field_ids(plan) if field_id in carried]


def _member_key_columns(
    plan: Mapping[str, Any], member: Mapping[str, Any]
) -> dict[str, str]:
    """canonical field id -> the physical column THIS member carries it in."""
    datastream_id = str(member["datastream_id"])
    columns: dict[str, str] = {}
    for edge in plan.get("edges") or []:
        for path in edge.get("key_paths") or []:
            field_id = str(path.get("canonical_field_id") or "")
            if str(edge.get("left")) == datastream_id:
                columns.setdefault(field_id, str(path.get("left_field") or ""))
            elif str(edge.get("right")) == datastream_id:
                columns.setdefault(field_id, str(path.get("right_field") or ""))
    return columns


def _relations(plan: Mapping[str, Any], relations: Mapping[str, str]) -> None:
    missing = [
        member["datastream_id"]
        for member in plan.get("members") or []
        if not relations.get(str(member["datastream_id"]))
    ]
    if missing:
        raise ExecutionRefused(
            "member_has_no_relation",
            "A source in this plan has no published output to read, so the analysis "
            "cannot be run yet.",
            detail=missing,
        )


def build_member_sql(
    plan: Mapping[str, Any],
    member: Mapping[str, Any],
    relation: str,
    shape: Mapping[str, Any] | None = None,
) -> tuple[str, list[Any]]:
    """One source, filtered and aggregated at the plan's grain. Never joined here.

    *shape* is what `resolve_member_shapes` read of the member's relation -- its
    columns, its long-form pair, its declared grain and its grain restrictions --
    and it makes this read THE read of a published Datastream
    (`execution-substrate.md`, 2026-09-05): promoted relation, superseded pulls, a
    long landing's measure summed under a condition on its own name, a shared
    landing restricted to this profile's own grain. Without a shape the member is
    read plainly, as before.
    """
    from core.query_execution import _superseded_source, breakdown_pivot  # noqa: PLC0415

    datastream_id = str(member["datastream_id"])
    shape = dict(shape or {})
    present = set(shape.get("present_columns") or [])
    long_form = shape.get("long_form")
    breakdown = breakdown_pivot(present) if present else None
    key_columns = _member_key_columns(plan, member)
    wanted_keys = _member_key_field_ids(plan, member)
    if not wanted_keys:
        raise ExecutionRefused(
            "member_has_no_join_key",
            f"{member.get('name')} carries no key of the selected relationship path.",
        )
    selects: list[str] = []
    breakdown_filters: list[tuple[str, str]] = []
    for field_id in wanted_keys:
        physical_key = key_columns[field_id]
        # A key that is a BREAKDOWN of this landing (`country` on a breakdown
        # relation) is a value in `breakdown_value`, restricted to its name.
        if breakdown and present and physical_key not in present:
            dimension_column, value_column = breakdown
            selects.append(f"{_safe_identifier(value_column)} AS {_alias('k', field_id)}")
            breakdown_filters.append((dimension_column, physical_key))
            continue
        selects.append(f"{_safe_identifier(physical_key)} AS {_alias('k', field_id)}")
    group_by = [str(index + 1) for index in range(len(wanted_keys))]
    params: list[Any] = []
    comparison = plan.get("comparison")
    comparison_column: str | None = None
    if isinstance(comparison, Mapping):
        comparison_field = str(comparison.get("canonical_field_id") or "")
        comparison_column = key_columns.get(comparison_field)
        if not comparison_column:
            raise ExecutionRefused(
                "comparison_field_not_carried",
                f"{member.get('name')} carries no governed time column for this comparison.",
            )
        current = comparison.get("current") or {}
        baseline = comparison.get("baseline") or {}
        column = _safe_identifier(comparison_column)
        selects.append(
            "CASE "
            f"WHEN {column} >= ? AND {column} <= ? THEN 'current' "
            f"WHEN {column} >= ? AND {column} <= ? THEN 'baseline' "
            f"END AS {COMPARISON_PERIOD_FIELD}"
        )
        params.extend(
            [
                current.get("start"),
                current.get("end"),
                baseline.get("start"),
                baseline.get("end"),
            ]
        )
        group_by.append(str(len(group_by) + 1))

    for measure in member.get("measures") or []:
        aggregation = str(measure.get("aggregation") or "sum").lower()
        if aggregation == "average":
            raise ExecutionRefused(
                "average_not_compilable_across_sources",
                f"{member.get('name')} contributes an averaged measure. An average of "
                "averages is not an average, and recomputing it needs the counts behind "
                "each one, which the mapping does not publish. Ask for its components.",
            )
        if aggregation not in COMPILABLE_AGGREGATIONS:
            raise ExecutionRefused(
                "measure_aggregation_not_compilable",
                f"{member.get('name')} contributes a measure aggregated as "
                f"{aggregation!r}, which cannot be merged safely.",
            )
        physical_name = str(measure["physical_field"])
        alias = _measure_name(measure)
        if long_form and present and physical_name not in present:
            # A measurement that arrived as a ROW is aggregated under a condition
            # on its own name -- the single-source pivot, verbatim.
            key_column, value_column = long_form
            selects.append(
                f"{aggregation.upper()}(IF({_safe_identifier(key_column)} = ?, "
                f"{_safe_identifier(value_column)}, NULL)) AS {alias}"
            )
            params.append(physical_name)
        else:
            selects.append(f"{aggregation.upper()}({_safe_identifier(physical_name)}) AS {alias}")
    # THE SOURCE IS THE SUPERSEDED RELATION, as the single-source read has it:
    # earlier pulls of a row dropped, the latest publication winning. Its own
    # parameters (none without an as-of) follow the SELECT's in text order.
    source, source_params = _superseded_source(relation, shape) if present else (relation, [])
    params.extend(source_params)
    predicates: list[str] = []
    for column, operator in shape.get("grain_restrictions") or []:
        predicates.append(f"{_safe_identifier(column)} {operator} ?")
        params.append("")
    for dimension_column, wanted_dimension in breakdown_filters:
        predicates.append(f"{_safe_identifier(dimension_column)} = ?")
        params.append(wanted_dimension)
    for filter_entry in plan.get("filters") or []:
        if filter_entry.get("stage") != "pre_aggregation":
            continue
        if str(filter_entry.get("datastream_id") or "") != datastream_id:
            continue
        field_id = str(filter_entry.get("canonical_field_id") or "")
        if (
            comparison_column
            and field_id == str(comparison.get("canonical_field_id") or "")
            and filter_entry.get("operator") in {"gte", "lte"}
        ):
            continue
        column = key_columns.get(field_id)
        if not column:
            raise ExecutionRefused(
                "filter_field_not_carried",
                f"{member.get('name')} carries no column for a filter this plan applies "
                "to it.",
            )
        operator = _OPERATORS.get(str(filter_entry.get("operator") or ""))
        if operator is None:
            raise ExecutionRefused(
                "filter_operator_not_compilable",
                f"{filter_entry.get('operator')!r} is not an operator this plan can run.",
            )
        predicates.append(f"{_safe_identifier(column)} {operator} ?")
        params.append(filter_entry.get("value"))

    if comparison_column and isinstance(comparison, Mapping):
        current = comparison.get("current") or {}
        baseline = comparison.get("baseline") or {}
        column = _safe_identifier(comparison_column)
        predicates.append(
            f"(({column} >= ? AND {column} <= ?) OR "
            f"({column} >= ? AND {column} <= ?))"
        )
        params.extend(
            [
                current.get("start"),
                current.get("end"),
                baseline.get("start"),
                baseline.get("end"),
            ]
        )

    where = f" WHERE {' AND '.join(predicates)}" if predicates else ""
    sql = (
        f"SELECT {', '.join(selects)} FROM {source}{where} "  # noqa: S608 - allowlisted
        f"GROUP BY {', '.join(group_by)}"
    )
    return sql, params


def build_sql(
    plan: Mapping[str, Any],
    relations: Mapping[str, str],
    shapes: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, list[Any]]:
    """The whole plan as one statement: aggregate each member, then merge."""
    members = list(plan.get("members") or [])
    if len(members) < 2:
        raise ExecutionRefused(
            "plan_has_one_member", "A cross-source execution needs at least two sources."
        )
    _relations(plan, relations)

    policy = str(plan.get("inclusion_policy") or "")
    join = _JOIN_FOR_POLICY.get(policy)
    if join is None:
        raise ExecutionRefused(
            "inclusion_policy_not_compilable",
            f"{policy!r} is not an inclusion policy this engine knows.",
        )

    primary_id = str(plan.get("primary_datastream_id") or members[0]["datastream_id"])
    member_by_id = {str(member["datastream_id"]): member for member in members}
    ordered_ids, traversal = _graph_traversal(plan, primary_id, set(member_by_id))
    ordered = [member_by_id[datastream_id] for datastream_id in ordered_ids]

    # A measure can only be broken down by dimensions its own published source
    # carries.  Joining A(day) through B(day,campaign) to C(campaign) and then
    # showing A.spend by campaign would repeat the day total once per campaign.
    # Until an explicit allocation contract exists, refuse that question rather
    # than returning a plausible doubled number.
    output_keys = set(output_dimension_field_ids(plan))
    for member in ordered:
        if member.get("measures") and not output_keys.issubset(
            _member_key_columns(plan, member)
        ):
            raise ExecutionRefused(
                "measure_not_defined_at_output_grain",
                f"{member.get('name')} does not carry every selected output dimension. "
                "Remove the foreign dimension or publish an explicit allocation contract.",
                detail={
                    "datastream_id": member["datastream_id"],
                    "missing_dimensions": sorted(
                        output_keys - set(_member_key_columns(plan, member))
                    ),
                },
            )

    ctes: list[str] = []
    params: list[Any] = []
    names: list[str] = []
    for index, member in enumerate(ordered):
        name = f"m{index}"
        member_sql, member_params = build_member_sql(
            plan,
            member,
            relations[str(member["datastream_id"])],
            (shapes or {}).get(str(member["datastream_id"])),
        )
        ctes.append(f"{name} AS ({member_sql})")
        params.extend(member_params)
        names.append(name)

    names_by_id = {
        str(member["datastream_id"]): names[index] for index, member in enumerate(ordered)
    }
    keys = output_dimension_field_ids(plan)
    key_selects = []
    for field_id in keys:
        alias = _alias("k", field_id)
        # COALESCE across every member: under `preserve_all` the key can arrive
        # from either side, and a bare `m0.k` would blank the rows only the other
        # source has -- which is exactly what the policy asked to preserve.
        carriers = [
            names_by_id[str(member["datastream_id"])]
            for member in ordered
            if field_id in _member_key_columns(plan, member)
        ]
        coalesced = ", ".join(f"{name}.{alias}" for name in carriers)
        key_selects.append(f"COALESCE({coalesced}) AS {alias}")
    comparison = plan.get("comparison")
    if isinstance(comparison, Mapping):
        period_sources = ", ".join(f"{name}.{COMPARISON_PERIOD_FIELD}" for name in names)
        key_selects.append(
            f"COALESCE({period_sources}) AS {COMPARISON_PERIOD_FIELD}"
        )

    measure_selects = []
    for index, member in enumerate(ordered):
        for measure in member.get("measures") or []:
            alias = _measure_name(measure)
            measure_selects.append(f"{names[index]}.{alias} AS {alias}")

    derived_selects, derived_params = _derived_selects(plan)
    params.extend(derived_params)

    joins = [names_by_id[primary_id]]
    for parent_id, child_id, edge in traversal:
        fields = [
            str(path.get("canonical_field_id") or "") for path in edge.get("key_paths") or []
        ]
        if not fields or any(not field_id for field_id in fields):
            raise ExecutionRefused(
                "edge_has_no_compilable_key", "A selected relationship edge carries no key."
            )
        on = " AND ".join(
            f"{names_by_id[parent_id]}.{_alias('k', field_id)} = "
            f"{names_by_id[child_id]}.{_alias('k', field_id)}"
            for field_id in fields
        )
        if isinstance(comparison, Mapping):
            on += (
                f" AND {names_by_id[parent_id]}.{COMPARISON_PERIOD_FIELD} = "
                f"{names_by_id[child_id]}.{COMPARISON_PERIOD_FIELD}"
            )
        joins.append(f"{join} {names_by_id[child_id]} ON {on}")

    selects = key_selects + measure_selects + derived_selects
    row_limit = int((plan.get("bounds") or {}).get("row_limit") or 10_000)
    order_fields = [*keys, *([COMPARISON_PERIOD_FIELD] if comparison else [])]
    if not order_fields:
        order_fields = [field["name"] for field in result_schema(plan)["fields"]]
    merged = (
        "WITH " + ", ".join(ctes) + " "  # noqa: S608 - every identifier is allowlisted
        f", merged AS (SELECT {', '.join(selects)} FROM " + " ".join(joins) + ") "
    )
    post_predicates, post_params = _post_filters(plan)
    params.extend(post_params)
    where = f" WHERE {' AND '.join(post_predicates)}" if post_predicates else ""
    sql = (
        merged + "SELECT * FROM merged" + where
        + " ORDER BY "
        + ", ".join(
            _alias("k", field_id) if field_id in keys else _safe_identifier(field_id)
            for field_id in order_fields
        )
        # Fetch one sentinel row so the Result can say that its analytical row
        # bound was reached. Returning exactly N rows cannot distinguish a
        # complete N-row answer from the first N rows of a larger answer.
        + f" LIMIT {int(row_limit) + 1}"
    )
    return sql, params


def _graph_traversal(
    plan: Mapping[str, Any], primary_id: str, member_ids: set[str]
) -> tuple[list[str], list[tuple[str, str, Mapping[str, Any]]]]:
    """Root the frozen tree at its primary member without reinterpreting its edges."""
    if primary_id not in member_ids:
        raise ExecutionRefused("primary_not_in_plan", "The plan's primary source is absent.")
    remaining = list(plan.get("edges") or [])
    joined = {primary_id}
    order = [primary_id]
    traversal: list[tuple[str, str, Mapping[str, Any]]] = []
    while remaining:
        candidates: list[tuple[str, str, Mapping[str, Any]]] = []
        for edge in remaining:
            left, right = str(edge.get("left") or ""), str(edge.get("right") or "")
            if left in joined and right not in joined:
                candidates.append((left, right, edge))
            elif right in joined and left not in joined:
                candidates.append((right, left, edge))
        if not candidates:
            raise ExecutionRefused(
                "join_graph_not_connected",
                "The frozen relationship tree cannot be traversed from its primary source.",
            )
        parent, child, edge = sorted(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
                str(item[2].get("common_key_version_id") or ""),
            ),
        )[0]
        remaining.remove(edge)
        joined.add(child)
        order.append(child)
        traversal.append((parent, child, edge))
    if joined != member_ids:
        raise ExecutionRefused(
            "join_graph_not_connected", "The frozen relationship tree omits a source."
        )
    return order, traversal


def _ratio_word(derived: Mapping[str, Any]) -> str:
    """The one word this module may print for a ratio. NEVER its canonical id.

    The executor holds a FROZEN plan and no connection to the vocabulary, so it
    cannot look a name up; the word is the one `multi_source_plan._field_word`
    resolved at compile time and froze onto the ratio, exactly as the unit is
    frozen there. A plan compiled before that word existed carries none, and the
    answer to "no word" is the ROLE, never the identifier -- an identifier names
    a row of a table the reader has no way to open.
    """
    from core.multi_source_plan import UNNAMED_MEASURE  # noqa: PLC0415

    return str(derived.get("canonical_name") or "").strip() or UNNAMED_MEASURE


def _derived_selects(plan: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """Ratios, recomputed from their governed components after the merge.

    `NULLIF(denominator, 0)` and not a CASE: a division by zero is not zero and
    not an error either -- it is a ratio that does not exist, and NULL is the one
    value that says so.
    """
    selects: list[str] = []
    by_field: dict[str, list[str]] = {}
    for member in plan.get("members") or []:
        for measure in member.get("measures") or []:
            by_field.setdefault(str(measure["canonical_field_id"]), []).append(
                _measure_name(measure)
            )
    for derived in plan.get("derived_measures") or []:
        numerator = str(derived.get("numerator_field_id") or "")
        denominator = str(derived.get("denominator_field_id") or "")
        if numerator not in by_field or denominator not in by_field:
            raise ExecutionRefused(
                "ratio_component_not_selected",
                f"{_ratio_word(derived)} is computed from two measures this analysis "
                "actually selected, and one of them is absent. Ask for both of its "
                "components as measures too, then run this analysis again.",
            )
        if len(by_field[numerator]) != 1 or len(by_field[denominator]) != 1:
            raise ExecutionRefused(
                "ratio_component_is_ambiguous",
                "A derived measure names a canonical component contributed by more than "
                "one Datastream. Select one source-qualified component.",
            )
        alias = _alias("r", str(derived.get("canonical_field_id") or ""))
        selects.append(
            f"{by_field[numerator][0]} / NULLIF({by_field[denominator][0]}, 0) AS {alias}"
        )
    return selects, []


def _post_filters(plan: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    predicates: list[str] = []
    params: list[Any] = []
    for filter_entry in plan.get("filters") or []:
        if filter_entry.get("stage") != "post_aggregation":
            continue
        field_id = str(filter_entry.get("canonical_field_id") or "")
        operator = _OPERATORS.get(str(filter_entry.get("operator") or ""))
        if operator is None:
            raise ExecutionRefused(
                "filter_operator_not_compilable",
                f"{filter_entry.get('operator')!r} is not an operator this plan can run.",
            )
        predicates.append(f"{_alias('k', field_id)} {operator} ?")
        params.append(filter_entry.get("value"))
    return predicates, params


# ---------------------------------------------------------------------------
# Running it, and writing the one immutable Result
# ---------------------------------------------------------------------------


def resolve_member_shapes(
    conn, *, project_id: str, plan: Mapping[str, Any], relations: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """What each member's relation IS -- read, never declared -- for `build_member_sql`.

    The same four readings the single-source resolver makes of a published
    Datastream (`query_execution.resolve_physical_plan`): the columns present,
    the long-form pair when measurements land as rows, the declared grain a wide
    landing supersedes on, and the axes that separate this profile from its
    siblings on a shared landing. A relation whose shape cannot be read gets no
    shape: the member is then read plainly, and the log says so.
    """
    from core import relation_shape  # noqa: PLC0415
    from core.query_execution import (  # noqa: PLC0415
        _grain_restrictions,
        _managed_feed_grain,
        long_form_pivot,
    )

    shapes: dict[str, dict[str, Any]] = {}
    for member in plan.get("members") or []:
        datastream_id = str(member["datastream_id"])
        qualified = relations.get(datastream_id)
        if not qualified:
            continue
        dataset, _, bare = qualified.rpartition(".")
        try:
            present = set(relation_shape.read(project_id, bare, dataset=dataset).columns)
        except Exception as exc:  # noqa: BLE001 -- an unknown shape is not an empty one
            logger.warning("multi_source_execution: shape_unreadable ds=%s: %s", datastream_id, exc)
            present = set()
        if not present:
            logger.warning(
                "multi_source_execution: member_read_plainly ds=%s relation=%s reason=shape_unreadable",
                datastream_id,
                bare,
            )
            continue
        shapes[datastream_id] = {
            "present_columns": sorted(present),
            "long_form": long_form_pivot(present),
            "grain_columns": _managed_feed_grain(conn, project_id, datastream_id),
            "grain_restrictions": _grain_restrictions(conn, project_id, datastream_id, present),
        }
    return shapes


def resolve_relations(conn, *, project_id: str, plan: Mapping[str, Any]) -> dict[str, str]:
    """Resolve only the exact Output version frozen in each member.

    Execution never asks for the latest Output. The scoped lookup below is a
    reauthorization and immutability check; the relation bytes must equal the
    server-owned bytes hashed into the plan.
    """
    from core.match_profile import _dataset_of  # noqa: PLC0415

    relations: dict[str, str] = {}
    for member in plan.get("members") or []:
        datastream_id = str(member["datastream_id"])
        output_version_id = str(member.get("output_version_id") or "")
        if not output_version_id:
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT relation_ref, execution_id, mapping_version_id, output_id,
                       plan_version_id, schema_hash
                  FROM app.datastream_output_versions
                 WHERE id = %s AND datastream_id = %s AND project_id = %s
                """,
                (output_version_id, datastream_id, project_id),
            )
            row = cur.fetchone()
        if row is None or not row[0]:
            continue
        expected = (
            str(member.get("published_execution_id") or ""),
            str(member.get("mapping_version_id") or ""),
            str(member.get("output_id") or ""),
            str(member.get("plan_version_id") or ""),
            member.get("schema_hash"),
        )
        observed = (str(row[1]), str(row[2]), str(row[3]), str(row[4]), row[5])
        if observed != expected or row[0] != member.get("relation_ref"):
            raise ExecutionRefused(
                "frozen_output_pin_mismatch",
                "An Output no longer matches the immutable version frozen in this plan.",
            )
        raw = row[0] if isinstance(row[0], str) else (row[0].get("relation") or "")
        # AI-308: A SKIP IS NOT AN ANSWER. A member left out of this map is still
        # in `ordered`, so `build_member_sql` reads `relations[datastream_id]` and
        # the run died on a bare KeyError -- uncaught, with the attempt already
        # accepted. And a member whose landing is not readable would have died one
        # line further down, inside `_safe_identifier`, the same way. Both are the
        # same fact and both are now an OUTCOME the caller can read.
        if not raw or not names_a_readable_relation(raw):
            raise ExecutionUnavailable(f"member {datastream_id} names no readable relation")
        # THE PROMOTED RELATION, after the pin check -- the rule the single-source
        # read applies (`query_execution`, AI-352/`raw_landing.promoted_relation`):
        # a `__cand_` table is the isolated copy of one publication, the shared
        # table is where every pull of the Datastream lives and supersedes.
        if "__cand_" in raw:
            from core.raw_landing import promoted_relation  # noqa: PLC0415

            raw = promoted_relation(raw)
        if "." in raw:
            relations[datastream_id] = ".".join(_safe_identifier(p) for p in raw.split("."))
            continue
        dataset = _dataset_of(project_id, raw)
        relations[datastream_id] = (
            f"{_safe_identifier(dataset)}.{_safe_identifier(raw)}"
            if dataset
            else _safe_identifier(raw)
        )
    return relations


def result_schema(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What the columns of the merged answer mean, in governed terms."""
    fields = [
        {"name": _alias("k", field_id), "canonical_field_id": field_id, "role": "dimension"}
        for field_id in output_dimension_field_ids(plan)
    ]
    if isinstance(plan.get("comparison"), Mapping):
        fields.append(
            {
                "name": COMPARISON_PERIOD_FIELD,
                "canonical_field_id": "comparison_period",
                "role": "dimension",
                "comparison": dict(plan["comparison"]),
            }
        )
    for member in plan.get("members") or []:
        for measure in member.get("measures") or []:
            fields.append(
                {
                    "name": _measure_name(measure),
                    "canonical_field_id": str(measure["canonical_field_id"]),
                    "role": "measure",
                    "aggregation": measure.get("aggregation"),
                    "datastream_id": member["datastream_id"],
                    # Frozen by the plan, carried by the Result: what the number
                    # is. A money column is canonical micros, and only its type
                    # and unit let a reader state 124 EUR instead of 124000000.
                    "value_type": measure.get("value_type"),
                    "unit": measure.get("unit"),
                }
            )
    for derived in plan.get("derived_measures") or []:
        fields.append(
            {
                "name": _alias("r", str(derived.get("canonical_field_id") or "")),
                "canonical_field_id": str(derived.get("canonical_field_id") or ""),
                "role": "ratio",
                "numerator_field_id": derived.get("numerator_field_id"),
                "denominator_field_id": derived.get("denominator_field_id"),
                "value_type": derived.get("value_type"),
                "unit": derived.get("unit"),
            }
        )
    return {"fields": fields}


def _finish(conn, *, defer_terminalization: bool, **terminal_kwargs: Any) -> dict[str, Any]:
    """Write the Result now, or hand it to the caller that owns the AI Path.

    The deferral helper is `query_execution`'s own, so a deferred cross-source
    Result is completed by exactly the same `complete_deferred_result` the
    single-source door already calls. Two deferral mechanisms would be two ways
    for an accepted attempt to end up without its Result.
    """
    from core.query_execution import _terminalize_or_defer  # noqa: PLC0415

    return _terminalize_or_defer(
        conn, defer_terminalization=defer_terminalization, **terminal_kwargs
    )


def _jsonable_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A Result payload is JSON, and a warehouse row is not.

    DuckDB hands back `datetime.date` and `Decimal`; BigQuery adds its own. The
    Result's content hash is taken over the serialized document, so the coercion
    has to happen HERE and once -- a value coerced differently on two paths would
    hash differently for the same answer.
    """
    import datetime as _datetime  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    coerced: dict[str, Any] = {}
    for name, value in row.items():
        if isinstance(value, (_datetime.date, _datetime.datetime)):
            coerced[name] = value.isoformat()
        elif isinstance(value, Decimal):
            # float and not str: a measure is a number downstream, and the pivot
            # of story 66.6 has to be able to add it.
            coerced[name] = float(value)
        else:
            coerced[name] = value
    return coerced


def _fit_result_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Keep whole rows under the Result payload budget; never cut a JSON value."""
    import json  # noqa: PLC0415

    fitted: list[dict[str, Any]] = []
    used = 2  # JSON array brackets.
    for row in rows:
        encoded = json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        separator = 1 if fitted else 0
        if len(encoded) + 2 > MAX_MULTI_SOURCE_ROWS_BYTES:
            raise ExecutionRefused(
                "result_row_exceeds_budget",
                "One aggregated row is too large to preserve safely. Narrow the selected "
                "dimensions before execution.",
            )
        if used + separator + len(encoded) > MAX_MULTI_SOURCE_ROWS_BYTES:
            return fitted, True
        fitted.append(row)
        used += separator + len(encoded)
    return fitted, False


def execute_plan(
    conn,
    *,
    org_id: str,
    project_id: str,
    query_spec_version_id: str,
    actor: str,
    attempt: dict[str, Any] | None = None,
    ai_path_id: str | None = None,
    defer_terminalization: bool = False,
) -> dict[str, Any]:
    """Run one frozen plan and write exactly one immutable Result.

    The attempt/Result machinery is `query_execution`'s, reused rather than
    reimplemented: a cross-source answer is a Result like any other, and a second
    Result writer would be a second definition of what an answer is.

    STORY 66.9 -- ONE EXECUTOR, TWO DOORS. `attempt`, `ai_path_id` and
    `defer_terminalization` exist so the MCP Analytics door can call THIS function
    instead of owning a second one. The model-facing door needs the attempt
    allocated before the AI Path opens, and the Result written after the path is
    finalized; both are the caller's business, and neither changes what is
    computed. Console passes none of the three and behaves exactly as before,
    which is what makes the two doors produce the same answer by construction
    rather than by two implementations agreeing.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from core import warehouse  # noqa: PLC0415
    from core.multi_source_plan import load_plan_version, merge_bound_verdict  # noqa: PLC0415
    from core.query_execution import accept_execution  # noqa: PLC0415

    loaded = load_plan_version(
        conn, project_id=project_id, query_spec_version_id=query_spec_version_id
    )
    plan = loaded["plan"]
    # THE BOUND IS READ AGAIN HERE, AND THAT IS THE POINT (AI-293). Compiling
    # inside the bound is a property of the plan the day it was frozen; a plan is
    # IMMUTABLE, so a plan frozen before the bound was evaluated on every path is
    # still out of bound today and used to execute anyway -- this function only
    # ever read `merge_bound` to decide whether evidence was optional. It is the
    # READING that refuses; the stored plan is not rewritten, and cannot be.
    #
    # BEFORE THE EVIDENCE GATES, deliberately. "Recompile from a fresh profile"
    # sends a person to measure duplication; a merge outside the bound would
    # still be outside it with the freshest evidence there is, and telling them
    # to go and refresh a profile first would name a gesture that repairs
    # nothing.
    verdict = merge_bound_verdict(conn, project_id=project_id, plan=plan)
    if not verdict["within"]:
        failure = verdict["failures"][0]
        raise ExecutionRefused(
            failure["condition"],
            failure["message"],
            detail={
                **(failure["detail"] or {}),
                "conditions_failed": [f["condition"] for f in verdict["failures"]],
                "merge_key_field_ids": verdict["merge_key_field_ids"],
                "output_field_ids": verdict["output_field_ids"],
            },
        )
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    if any(
        edge.get("measured_safety") != "ready" and not edge.get("safety_mitigation")
        for edge in plan.get("edges") or []
    ):
        raise ExecutionRefused(
            "profile_not_ready",
            "This frozen plan has no executable matching-safety evidence. Recompile it "
            "from the current match profile before running it.",
        )
    # WHAT THE EXPIRY PROTECTS, AND WHERE IT STOPS APPLYING (AI-293). This gate
    # exists so changed source rows cannot reuse an old duplication measurement.
    # A plan the merge bound ADMITTED made no decision from rows at all: its
    # permission is that the merge key equals the aggregation grain and every
    # combined metric is additive, and neither of those can be falsified by rows
    # arriving. So an edge that carries evidence is still held to it -- stale
    # evidence is never read as fresh -- while an edge the bound admitted without
    # any is not asked for evidence it was never required to have.
    bound_lifted = set((plan.get("merge_bound") or {}).get("refusals_lifted") or [])
    evidence_optional = "profile_required" in bound_lifted
    for edge in plan.get("edges") or []:
        evidence = edge.get("profile_evidence") or {}
        valid_until = evidence.get("valid_until")
        if not isinstance(valid_until, int):
            if evidence_optional:
                continue
            raise ExecutionRefused(
                "profile_expired",
                "The matching evidence expired before execution. Recompile from a fresh "
                "profile so changed source rows cannot reuse an old safety decision.",
            )
        if int(valid_until) < now_epoch:
            raise ExecutionRefused(
                "profile_expired",
                "The matching evidence expired before execution. Recompile from a fresh "
                "profile so changed source rows cannot reuse an old safety decision.",
            )
    started_at = datetime.now(timezone.utc)
    if attempt is None:
        attempt = accept_execution(
            conn,
            org_id=org_id,
            project_id=project_id,
            query_spec_version_id=query_spec_version_id,
            actor=actor,
        )

    grain_field_id = plan.get("grain_canonical_field_id")
    time_filters = [
        entry
        for entry in plan.get("filters") or []
        if grain_field_id
        and entry.get("canonical_field_id") == grain_field_id
        and entry.get("operator") in {"gte", "lte"}
    ]
    starts = sorted(
        {str(entry.get("value")) for entry in time_filters if entry["operator"] == "gte"}
    )
    ends = sorted({str(entry.get("value")) for entry in time_filters if entry["operator"] == "lte"})
    manifest = {
        "plan_content_hash": loaded["content_hash"],
        "analysis_context": plan.get("analysis_context"),
        "inclusion_policy": plan.get("inclusion_policy"),
        "grain": plan.get("grain"),
        "time_window": {
            "start": starts[0] if len(starts) == 1 else None,
            "end": ends[0] if len(ends) == 1 else None,
        },
        "comparison": plan.get("comparison"),
        "filters": [
            f"{entry.get('canonical_field_id')} {entry.get('operator')} {entry.get('value')}"
            for entry in plan.get("filters") or []
        ],
        "limits": {"row_limit": (plan.get("bounds") or {}).get("row_limit")},
        "members": [
            {
                "datastream_id": member["datastream_id"],
                "name": member.get("name"),
                "mapping_version_id": member["mapping_version_id"],
                "output_version_id": member.get("output_version_id"),
                "output_id": member.get("output_id"),
                "published_execution_id": member.get("published_execution_id"),
                "publication_log_id": member.get("publication_log_id"),
                "plan_version_id": member.get("plan_version_id"),
                "relation_ref": member.get("relation_ref"),
                "schema_hash": member.get("schema_hash"),
            }
            for member in plan.get("members") or []
        ],
        "edges": [
            {
                "left": edge["left"],
                "right": edge["right"],
                "common_key_version_id": edge["common_key_version_id"],
                "relationship_name": edge["relationship"]["relationship_name"],
                "view_version_id": edge["relationship"]["view_version_id"],
            }
            for edge in plan.get("edges") or []
        ],
    }

    try:
        relations = resolve_relations(conn, project_id=project_id, plan=plan)
        shapes = resolve_member_shapes(conn, project_id=project_id, plan=plan, relations=relations)
        sql, params = build_sql(plan, relations, shapes)
    except ExecutionUnavailable as exc:
        # AI-308: could-not-compile is `unavailable`, never an exception thrown at
        # a caller whose attempt this function already accepted. The refused
        # identifier is evidence for a repairer, so it goes to the log; the answer
        # carries the sentence that names the gesture.
        logger.warning(
            "multi_source_execution: unreadable_identifier: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return _finish(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="unavailable",
            started_at=started_at,
            rows=[],
            result_schema=result_schema(plan),
            manifest={
                **manifest,
                "unavailable_reason": UNREADABLE_LANDING_MESSAGE,
                "missing_link": "physical_identifier",
            },
            ai_path_id=ai_path_id,
        )
    except ExecutionRefused as exc:
        # A refusal is a Result, not an exception thrown at a caller: the attempt
        # already exists and AC4 of story 50.1 says an accepted attempt never
        # stays without its Result.
        return _finish(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="refused",
            started_at=started_at,
            rows=[],
            result_schema=result_schema(plan),
            manifest={**manifest, "refusal": {"code": exc.code, "message": exc.message}},
            ai_path_id=ai_path_id,
        )

    bigquery_mode = warehouse._db_mode() == "bigquery"
    runner = warehouse._query_bigquery if bigquery_mode else warehouse._query_duckdb
    if bigquery_mode:
        from core.query_execution import _bind_positional  # noqa: PLC0415

        sql = _bind_positional(sql)

    # AR8, la moitie ESTIMATION. Les limites de securite etaient calculees avant
    # l'execution -- plafond de lignes, budget en octets, preflight de
    # pivotabilite -- mais rien ne disait ce que la requete allait SCANNER. Un
    # `dry_run` BigQuery le dit gratuitement : il ne lance rien et ne facture
    # rien. L'estimation part avec le Result, a cote de ce que l'execution a
    # reellement coute, pour qu'on puisse comparer les deux.
    #
    # ELLE NE BLOQUE PAS. Ne pas savoir ce qu'une requete coutera n'est pas une
    # raison de refuser de repondre : `estimate_scan` ne leve jamais et rend
    # `unavailable` en nommant la panne.
    scan_estimate = warehouse.estimate_scan(sql, params).as_dict()
    manifest = {**manifest, "scan_estimate": scan_estimate}

    try:
        rows = runner(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "multi_source_execution: warehouse_unreadable: %s: %s", type(exc).__name__, exc
        )
        # Could not ask is `unavailable`, never `empty`: claiming nothing matched
        # would be a statement about the data we never obtained.
        return _finish(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="unavailable",
            started_at=started_at,
            rows=[],
            result_schema=result_schema(plan),
            manifest={**manifest, "unavailable_reason": "the warehouse could not be read"},
            ai_path_id=ai_path_id,
        )

    try:
        row_limit = int((plan.get("bounds") or {}).get("row_limit") or 10_000)
        row_limit_truncated = len(rows) > row_limit
        bounded_rows = rows[:row_limit]
        rows, byte_truncated = _fit_result_rows(
            [_jsonable_row(row) for row in bounded_rows]
        )
        payload_truncated = row_limit_truncated or byte_truncated
    except ExecutionRefused as exc:
        return _finish(
            conn,
            defer_terminalization=defer_terminalization,
            attempt=attempt,
            org_id=org_id,
            project_id=project_id,
            outcome="refused",
            started_at=started_at,
            rows=[],
            result_schema=result_schema(plan),
            manifest={**manifest, "refusal": {"code": exc.code, "message": exc.message}},
            ai_path_id=ai_path_id,
        )
    return _finish(
        conn,
        defer_terminalization=defer_terminalization,
        attempt=attempt,
        org_id=org_id,
        project_id=project_id,
        outcome="success" if rows else "empty",
        started_at=started_at,
        rows=rows,
        truncated=payload_truncated,
        result_schema=result_schema(plan),
        manifest={**manifest, "sql_shape": "aggregate_then_merge"},
        ai_path_id=ai_path_id,
    )
