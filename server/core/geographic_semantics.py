"""Read-layer geographic grouping, pinned to one governed hierarchy version.

Raw and canonical fact rows are immutable inputs. This module pins the exact
``country`` partition, canonicalizes source aliases, and returns new semantic
rows for assigned Markets and Rest of World. Unknown remains separate DQ evidence
and is excluded from primary segments, never hidden in Other or shown as a third
segment. No fact is ever rewritten:
regrouping is a new hierarchy version, and the next read simply answers
differently (Story 48.2, AC7).

What changed in Story 48.2, and why it is not a refactor:

* the split was driven by ``project_preferences.local_markets`` -- a mutable
  JSON list with no version. Two identical reports run a week apart could not be
  compared, because nothing recorded which grouping produced which. Every row
  now carries ``geography_hierarchy_version_id``;
* ``__other_markets__`` was a fixed identity with a fixed English label and no
  way down. Rest of World is now a real node: renameable, placeable in the
  hierarchy, and drillable to the exact country rows it stands for;
* there was no Region. ``Country -> Market -> Region`` resolves in one pass;
* Unknown absorbed nothing but unresolved values then, and still absorbs nothing
  else now -- that separation is the one thing here that was already right.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from core.country_registry import GeographyProjection
    from core.geographic_conformance import CountryResolver

from core import report_dictionary
from core.country_registry import (
    BUCKET_ASSIGNED,
    BUCKET_REST_OF_WORLD,
    BUCKET_UNKNOWN,
)
from core.country_vocabulary import CANONICAL_COUNTRY_DIMENSION, normalize_country_value

#: Unknown is an evidence state, not a Master Data node, so it keeps a reserved
#: identity rather than a governed one. Nothing may move a country into it.
#:
#: THE LABEL SAYS WHAT HAPPENED, since story 58.5. It was the bare word `Unknown`,
#: which reads exactly like a place -- a reader scanning a country list has no way to
#: tell it from a country nobody has heard of, and the plan forbids that for the
#: absence buckets of this dimension. It is not a rename for taste: the two buckets
#: of this file are the only two things here that are NOT countries, and they now say
#: so in the same voice. Nothing keys on the text: `UNKNOWN_BUCKET_ID` is the
#: identity, and `geography_bucket_kind` is what a caller branches on.
UNKNOWN_BUCKET_ID = "__unknown__"
UNKNOWN_BUCKET_LABEL = "Not resolved to a country"
OTHER_BUCKET_ID = "__other__"
COUNTRY_PARTITION = CANONICAL_COUNTRY_DIMENSION

#: THE ROW THE SOURCE GAVE NO COUNTRY FOR -- story 58.5, arbitrage 1.
#:
#: THE SAME LITERAL LIVES IN THE MART, and it has to: `dbt/macros/country_absence.sql`
#: writes this exact string into `fact_daily_kpi.breakdown_value` so a row with no
#: country signal is KEPT under a name instead of vanishing (int_country_daily_kpi) or
#: turning `breakdown_value` NULL by concatenation (fact_daily_kpi). The two spellings
#: are one, and `tests/core/test_geographic_semantics.py` reads the macro to prove it
#: -- a sentinel the reader spells differently from the writer is a bucket nobody can
#: find.
#:
#: AND IT IS NOT `Unknown`. Those are two facts and two desks:
#:
#:   * `unknown` -- the provider wrote something this vocabulary cannot resolve. The
#:     repair is a governed conformance mapping, and the value is the evidence for it.
#:   * `country_absent` -- the provider wrote NOTHING. There is no value to map and no
#:     conformance mapping that could ever fire; the repair, if there is one, is in the
#:     report the provider is asked for. Filing it under `unknown` would queue work on
#:     a desk that cannot do it.
#:
#: IT IS NOT A COUNTRY EITHER, and it can never become one: `country_vocabulary`
#: refuses any code that is not `[A-Z]{2}`, so this identity cannot enter the ISO set,
#: and every reader qualifies it by its KIND before its label reaches a screen.
COUNTRY_ABSENT_BUCKET_ID = "__country_absent__"
COUNTRY_ABSENT_BUCKET_LABEL = "No country reported"
COUNTRY_ABSENT_BUCKET_KIND = "country_absent"

#: The finding an absent country raises. `country_value_unmapped` is deliberately NOT
#: reused: its `repair_path` sends a person to the conformance surface, where there
#: would be nothing to conform.
COUNTRY_ABSENT_FINDING = "country_absent_at_source"


def is_country_absent_bucket(value: object) -> bool:
    """Is this mart value the declared no-country bucket rather than a place?"""

    return isinstance(value, str) and value == COUNTRY_ABSENT_BUCKET_ID


class GeographicAggregationError(ValueError):
    """A non-additive metric lacks the evidence required by its semantic rule."""


@dataclass(frozen=True, slots=True)
class GeographyGroupingResult:
    rows: tuple[dict, ...]
    data_quality: tuple[dict, ...]
    country_partition: str
    excluded_parallel_rows: int
    hierarchy_version_id: str


def _require_number(row: Mapping[str, object], field: str, metric: str) -> float:
    raw = row.get(field)
    if raw is None:
        raise GeographicAggregationError(
            f"metric {metric!r} requires {field} for geographic aggregation"
        )
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise GeographicAggregationError(f"metric {metric!r} has invalid {field} evidence") from exc


def _aggregate(group: Sequence[Mapping[str, object]], metric: str) -> tuple[float, str]:
    """Apply the metric's governed rule AFTER grouping, and fail closed.

    A non-additive metric that is summed because its evidence was missing is a
    wrong number presented as a right one, which is worse than no number.
    """

    rule = report_dictionary.aggregation_rule(metric)
    if not rule or rule == "sum":
        return sum(float(row.get("value") or 0) for row in group), "sum"
    if rule == "max":
        return max(_require_number(row, "value", metric) for row in group), rule
    if rule in {"ratio", "weighted_ratio"}:
        numerator = sum(_require_number(row, "semantic_numerator", metric) for row in group)
        denominator = sum(_require_number(row, "semantic_denominator", metric) for row in group)
        if denominator == 0:
            raise GeographicAggregationError(
                f"metric {metric!r} has a zero semantic_denominator after grouping"
            )
        return numerator / denominator, rule
    if rule in {"impression_weighted_average", "impression_weighted"}:
        weighted_sum = 0.0
        weight_sum = 0.0
        for row in group:
            weight = _require_number(row, "semantic_weight", metric)
            weighted_sum += float(row.get("value") or 0) * weight
            weight_sum += weight
        if weight_sum == 0:
            raise GeographicAggregationError(
                f"metric {metric!r} has zero semantic_weight after grouping"
            )
        return weighted_sum / weight_sum, rule
    raise GeographicAggregationError(
        f"metric {metric!r} has unsupported geographic aggregation rule {rule!r}"
    )


def _resolve(
    raw_value: object, resolver: "CountryResolver | None", connector: object
) -> str | None:
    """The governed two-step: the platform vocabulary, then confirmed mappings.

    Because this runs at read time, confirming a conformance mapping
    reclassifies retained rows on the next read with no fact rewrite.
    """

    if resolver is not None:
        return resolver(raw_value, connector)
    return normalize_country_value(raw_value)


def _bucket_identity(bucket: Mapping[str, object]) -> tuple[str, str]:
    kind = str(bucket.get("geography_bucket_kind"))
    if kind == BUCKET_UNKNOWN:
        return UNKNOWN_BUCKET_ID, UNKNOWN_BUCKET_LABEL
    identity = bucket.get("market_id")
    label = bucket.get("market_label")
    return str(identity), str(label if label is not None else identity)


def group_geography_reporting_rows(
    rows: Iterable[Mapping[str, object]],
    projection: "GeographyProjection",
    *,
    resolver: "CountryResolver | None" = None,
) -> GeographyGroupingResult:
    """Return version-pinned semantic rows without mutating retained facts.

    Only exact ``breakdown_dimension='country'`` rows participate. That pins a
    single partition and prevents a total from summing parallel or composite
    breakdown series -- an error that produces a plausible number, which is the
    kind that survives review.
    """

    materialized = list(rows)
    country_rows = [
        row for row in materialized if row.get("breakdown_dimension") == COUNTRY_PARTITION
    ]
    if not country_rows:
        return GeographyGroupingResult(
            rows=tuple(dict(row) for row in materialized),
            data_quality=(),
            country_partition=COUNTRY_PARTITION,
            excluded_parallel_rows=0,
            hierarchy_version_id=projection.hierarchy_version_id,
        )

    groups: dict[tuple[object, ...], list[Mapping[str, object]]] = {}
    bucket_meta: dict[tuple[object, ...], dict[str, object]] = {}
    dq: list[dict] = []
    identity_fields = ("project_id", "date", "connector", "metric", "pull_id", "loaded_at")

    for row in country_rows:
        raw_value = row.get("breakdown_value")
        connector = row.get("connector")
        if is_country_absent_bucket(raw_value):
            # Story 58.5, arbitrage 1. The mart KEEPS this row now, so the reading
            # has to answer for it -- and with its own words. Sent through the
            # resolver it would land in `unknown` and queue a conformance mapping
            # for a value that does not exist; its contribution would also be
            # reported as unresolved evidence, which it is not. It is a fact about
            # the SOURCE.
            dq.append(
                {
                    "code": COUNTRY_ABSENT_FINDING,
                    "message": (
                        "The source reported no country on these rows. There is "
                        "nothing to map: the value is absent, not unrecognised, and "
                        "the rows are retained under the declared no-country bucket "
                        "so the country total still equals the day total."
                    ),
                    "raw_value": None,
                    "connector": connector,
                    "pull_id": row.get("pull_id"),
                    "canonical_dimension": COUNTRY_PARTITION,
                    # No repair path: the conformance surface has no lever here.
                    "repair_path": None,
                    "source_row": {
                        field: row.get(field)
                        for field in (
                            "project_id",
                            "date",
                            "connector",
                            "metric",
                            "breakdown_dimension",
                            "breakdown_value",
                            "value",
                            "pull_id",
                            "loaded_at",
                        )
                        if row.get(field) is not None
                    },
                    "geography_hierarchy_version_id": projection.hierarchy_version_id,
                }
            )
            continue
        canonical = _resolve(raw_value, resolver, connector)
        bucket = projection.bucket_for(canonical)
        if bucket["geography_bucket_kind"] == BUCKET_UNKNOWN:
            dq.append(
                {
                    "code": "country_value_unmapped",
                    "message": (
                        "Country value could not be mapped to the canonical vocabulary. "
                        "Resolve it once as a governed conformance mapping; the shared "
                        "vocabulary is never edited for one client."
                    ),
                    "raw_value": raw_value,
                    "connector": connector,
                    "pull_id": row.get("pull_id"),
                    "canonical_dimension": COUNTRY_PARTITION,
                    "repair_path": "dimension_conformance",
                    # Minimized row provenance: enough to reconcile the residual
                    # and repair the value, without copying arbitrary source fields.
                    "source_row": {
                        field: row.get(field)
                        for field in (
                            "project_id",
                            "date",
                            "connector",
                            "metric",
                            "breakdown_dimension",
                            "breakdown_value",
                            "value",
                            "pull_id",
                            "loaded_at",
                        )
                        if row.get(field) is not None
                    },
                    "geography_hierarchy_version_id": projection.hierarchy_version_id,
                }
            )
            # Unknown is a separate evidence channel. It is neither a valid
            # untracked country nor a primary reporting segment.
            continue
        identity, _label = _bucket_identity(bucket)
        key = tuple(row.get(field) for field in identity_fields) + (identity,)
        groups.setdefault(key, []).append(row)
        bucket_meta[key] = bucket

    def _sort_key(item: tuple[tuple[object, ...], list[Mapping[str, object]]]) -> tuple[str, ...]:
        key = item[0]
        identity = str(key[-1])
        bucket = bucket_meta[key]
        kind = str(bucket.get("geography_bucket_kind"))
        # Assigned Markets first, then the residual, then the repair bucket:
        # the reading order of the reconciliation itself.
        rank = "0" if kind == BUCKET_ASSIGNED else "1" if kind == BUCKET_REST_OF_WORLD else "2"
        return tuple("" if part is None else str(part) for part in key[:-1]) + (rank, identity)

    output: list[dict] = []
    for key, grouped in sorted(groups.items(), key=_sort_key):
        first = dict(grouped[0])
        bucket = bucket_meta[key]
        identity, label = _bucket_identity(bucket)
        value, rule = _aggregate(grouped, str(first.get("metric") or ""))
        first.update(
            {
                "breakdown_dimension": "market",
                "breakdown_value": identity,
                "value": value,
                "market_id": identity,
                "market_label": label,
                "market_kind": bucket["geography_bucket_kind"],
                "region_id": bucket.get("region_id"),
                "region_label": bucket.get("region_label"),
                "geography_bucket_kind": bucket["geography_bucket_kind"],
                "geography_hierarchy_version_id": projection.hierarchy_version_id,
                "semantic_aggregation_rule": rule,
            }
        )
        for transient in ("semantic_numerator", "semantic_denominator", "semantic_weight"):
            first.pop(transient, None)
        output.append(first)

    return GeographyGroupingResult(
        rows=tuple(output),
        data_quality=tuple(dq),
        country_partition=COUNTRY_PARTITION,
        excluded_parallel_rows=len(materialized) - len(country_rows),
        hierarchy_version_id=projection.hierarchy_version_id,
    )


def drill_rest_of_world(
    rows: Iterable[Mapping[str, object]],
    projection: "GeographyProjection",
    *,
    resolver: "CountryResolver | None" = None,
) -> tuple[dict, ...]:
    """The country rows Rest of World stands for, with no provider pull (AC5).

    The detail was always retained; only the grouping hid it. Drilling is
    therefore a read, never a request for data -- which is exactly why moving a
    country out of Rest of World needs no backfill either.
    """

    residual = set(projection.rest_of_world_members())
    detail: list[dict] = []
    for row in rows:
        if row.get("breakdown_dimension") != COUNTRY_PARTITION:
            continue
        canonical = _resolve(row.get("breakdown_value"), resolver, row.get("connector"))
        if canonical is None or canonical not in residual:
            continue
        enriched = dict(row)
        enriched.update(
            {
                "country_id": canonical,
                "source_country_value": row.get("breakdown_value"),
                "geography_bucket_kind": BUCKET_REST_OF_WORLD,
                "geography_hierarchy_version_id": projection.hierarchy_version_id,
            }
        )
        detail.append(enriched)
    return tuple(detail)


def geography_bucket_descriptors(projection: "GeographyProjection") -> list[dict[str, object]]:
    """Accessible descriptors for the governed split.

    ``bindable`` marks what a budget or objective may bind to: never Rest of
    World, whose membership changes whenever a Market does, and never Unknown,
    which is a repair queue rather than a place.
    """

    descriptors = [
        {**descriptor, "primary_report_segment": True}
        for descriptor in projection.descriptors()
    ]
    descriptors.append(
        {
            "id": UNKNOWN_BUCKET_ID,
            "label": UNKNOWN_BUCKET_LABEL,
            "kind": BUCKET_UNKNOWN,
            "bindable": False,
            "primary_report_segment": False,
        }
    )
    return descriptors


def reconciliation(
    result: GeographyGroupingResult, metric: str | None = None
) -> dict[str, object]:
    """Prove selected Markets plus Other equals the Country-grain total.

    Unknown is a separate DQ contribution, not a subset of Other and not a
    third primary segment. The resolved and source totals are both stated.
    Non-additive metrics are named and never falsely summed.
    """

    totals = {BUCKET_ASSIGNED: 0.0, BUCKET_REST_OF_WORLD: 0.0}
    additive = True
    for row in result.rows:
        if metric is not None and row.get("metric") != metric:
            continue
        if row.get("semantic_aggregation_rule") not in (None, "sum"):
            additive = False
            continue
        kind = str(row.get("geography_bucket_kind") or "")
        if kind in totals:
            totals[kind] += float(row.get("value") or 0)
    unknown_contribution = 0.0
    # COUNTED APART, story 58.5. Both stay outside the primary segments, but
    # "we could not read this value" and "there was no value" are two different
    # amounts of work and folding them into one number hides the smaller one.
    country_absent_contribution = 0.0
    for finding in result.data_quality:
        source_row = finding.get("source_row") or {}
        if metric is not None and source_row.get("metric") != metric:
            continue
        if finding.get("code") == COUNTRY_ABSENT_FINDING:
            country_absent_contribution += float(source_row.get("value") or 0)
        else:
            unknown_contribution += float(source_row.get("value") or 0)
    resolved_total = totals[BUCKET_ASSIGNED] + totals[BUCKET_REST_OF_WORLD]
    # The source total still accounts for EVERY country row, whichever channel it
    # left through -- that identity is the whole point of the reconciliation.
    source_total = (
        resolved_total + unknown_contribution + country_absent_contribution
        if additive
        else None
    )
    return {
        "additive": additive,
        "assigned": totals[BUCKET_ASSIGNED],
        "rest_of_world": totals[BUCKET_REST_OF_WORLD],
        "unknown_contribution": unknown_contribution if additive else None,
        "unknown": unknown_contribution if additive else None,
        "country_absent_contribution": (
            country_absent_contribution if additive else None
        ),
        "resolved_total": resolved_total,
        "source_total": source_total,
        "total": source_total,
        "geography_hierarchy_version_id": result.hierarchy_version_id,
    }
