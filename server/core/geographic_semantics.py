"""Read-layer market grouping for Local markets projects (CAP-27).

Raw and canonical fact rows are immutable inputs.  This module pins the exact
``country`` partition, canonicalizes source aliases, and returns new semantic
rows for tracked markets, Other markets, and Unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from core import report_dictionary
from core.country_vocabulary import normalize_country_value
from core.geographic_reporting import LOCAL_MARKETS, GeographicPosture

OTHER_MARKETS = "__other_markets__"
UNKNOWN_MARKET = "__unknown_market__"
COUNTRY_PARTITION = "country"


class GeographicAggregationError(ValueError):
    """A non-additive metric lacks the evidence required by its semantic rule."""


@dataclass(frozen=True, slots=True)
class MarketGroupingResult:
    rows: tuple[dict, ...]
    data_quality: tuple[dict, ...]
    country_partition: str
    excluded_parallel_rows: int


def _bucket(raw_value: object, tracked: frozenset[str]) -> tuple[str, str, str | None, str]:
    canonical = normalize_country_value(raw_value)
    if canonical is None:
        return UNKNOWN_MARKET, "Unknown", None, "unknown"
    if canonical in tracked:
        return canonical, canonical, canonical, "tracked"
    return OTHER_MARKETS, "Other markets", None, "other"


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
    rule = report_dictionary.aggregation_rule(metric)
    if not rule or rule == "sum":
        return sum(float(row.get("value") or 0) for row in group), "sum"
    if rule == "max":
        # Fail closed like the other non-additive rules: a NULL value is absent
        # evidence, not a real 0, so it must not silently win/lose a max.
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


def group_market_reporting_rows(
    rows: Iterable[Mapping[str, object]],
    posture: GeographicPosture,
) -> MarketGroupingResult:
    """Return Local-market semantic rows without mutating retained fact rows.

    Only exact ``breakdown_dimension='country'`` rows participate.  This pins a
    single partition and prevents totals from summing parallel or composite
    breakdown series. Global posture is intentionally a no-op.
    """

    materialized = list(rows)
    if posture.mode != LOCAL_MARKETS:
        return MarketGroupingResult(
            rows=tuple(dict(row) for row in materialized),
            data_quality=(),
            country_partition=COUNTRY_PARTITION,
            excluded_parallel_rows=0,
        )

    country_rows = [
        row for row in materialized if row.get("breakdown_dimension") == COUNTRY_PARTITION
    ]
    if not country_rows:
        return MarketGroupingResult(
            rows=tuple(dict(row) for row in materialized),
            data_quality=(),
            country_partition=COUNTRY_PARTITION,
            excluded_parallel_rows=0,
        )

    tracked = frozenset(posture.country_codes)
    groups: dict[tuple[object, ...], list[Mapping[str, object]]] = {}
    bucket_meta: dict[tuple[object, ...], tuple[str, str | None, str]] = {}
    dq: list[dict] = []
    identity_fields = ("project_id", "date", "connector", "metric", "pull_id", "loaded_at")

    for row in country_rows:
        raw_value = row.get("breakdown_value")
        bucket_id, label, market_code, kind = _bucket(raw_value, tracked)
        if kind == "unknown":
            dq.append(
                {
                    "code": "country_value_unmapped",
                    "message": "Country value could not be mapped to the canonical vocabulary.",
                    "raw_value": raw_value,
                    "connector": row.get("connector"),
                    "pull_id": row.get("pull_id"),
                }
            )
        key = tuple(row.get(field) for field in identity_fields) + (bucket_id,)
        groups.setdefault(key, []).append(row)
        bucket_meta[key] = (label, market_code, kind)

    def _sort_key(item: tuple[tuple[object, ...], list[Mapping[str, object]]]) -> tuple[str, ...]:
        key = item[0]
        bucket = str(key[-1])
        bucket_rank = "1" if bucket == OTHER_MARKETS else "2" if bucket == UNKNOWN_MARKET else "0"
        return tuple("" if part is None else str(part) for part in key[:-1]) + (bucket_rank, bucket)

    output: list[dict] = []
    for key, grouped in sorted(groups.items(), key=_sort_key):
        first = dict(grouped[0])
        bucket_id = str(key[-1])
        label, market_code, kind = bucket_meta[key]
        value, rule = _aggregate(grouped, str(first.get("metric") or ""))
        first.update(
            {
                "breakdown_dimension": "market",
                "breakdown_value": bucket_id,
                "value": value,
                "market_label": label,
                "market_kind": kind,
                "market_code": market_code,
                "semantic_aggregation_rule": rule,
            }
        )
        for transient in ("semantic_numerator", "semantic_denominator", "semantic_weight"):
            first.pop(transient, None)
        output.append(first)

    return MarketGroupingResult(
        rows=tuple(output),
        data_quality=tuple(dq),
        country_partition=COUNTRY_PARTITION,
        excluded_parallel_rows=len(materialized) - len(country_rows),
    )


def market_bucket_descriptors(posture: GeographicPosture) -> list[dict[str, object]]:
    """Accessible descriptors for the default Local-market split."""

    if posture.mode != LOCAL_MARKETS:
        return []
    tracked = [
        {"id": code, "label": code, "kind": "tracked", "country_code": code}
        for code in posture.country_codes
    ]
    return tracked + [
        {"id": OTHER_MARKETS, "label": "Other markets", "kind": "other", "country_code": None},
        {"id": UNKNOWN_MARKET, "label": "Unknown", "kind": "unknown", "country_code": None},
    ]
