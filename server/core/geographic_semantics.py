"""Read-layer market grouping for Local markets projects (CAP-27).

Raw and canonical fact rows are immutable inputs.  This module pins the exact
``country`` partition, canonicalizes source aliases, and returns new semantic
rows for tracked markets, Other markets, and Unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from core.geographic_conformance import CountryResolver

from core import report_dictionary
from core.country_vocabulary import CANONICAL_COUNTRY_DIMENSION, normalize_country_value
from core.geographic_reporting import LOCAL_MARKETS, GeographicPosture, Market

OTHER_MARKETS = "__other_markets__"
UNKNOWN_MARKET = "__unknown_market__"
OTHER_MARKETS_LABEL = "Other markets"
UNKNOWN_MARKET_LABEL = "Unknown"
COUNTRY_PARTITION = CANONICAL_COUNTRY_DIMENSION


class GeographicAggregationError(ValueError):
    """A non-additive metric lacks the evidence required by its semantic rule."""


@dataclass(frozen=True, slots=True)
class MarketGroupingResult:
    rows: tuple[dict, ...]
    data_quality: tuple[dict, ...]
    country_partition: str
    excluded_parallel_rows: int


@dataclass(frozen=True, slots=True)
class _Bucket:
    id: str
    label: str
    kind: str
    country_codes: tuple[str, ...]

    @property
    def market_code(self) -> str | None:
        """Historical single-code field, kept for pre-37.8 payload readers."""

        return self.country_codes[0] if len(self.country_codes) == 1 else None


_OTHER_BUCKET = _Bucket(OTHER_MARKETS, OTHER_MARKETS_LABEL, "other", ())
_UNKNOWN_BUCKET = _Bucket(UNKNOWN_MARKET, UNKNOWN_MARKET_LABEL, "unknown", ())


def _market_index(posture: GeographicPosture) -> dict[str, Market]:
    """Country code -> owning market. Disjunction is guaranteed upstream."""

    return {
        code: market for market in posture.markets for code in market.country_codes
    }


def _bucket(
    raw_value: object,
    index: Mapping[str, Market],
    resolver: "CountryResolver | None" = None,
    connector: object = None,
) -> _Bucket:
    """Classify a country row by **market membership**, not by code equality.

    Resolution is the governed two-step of Story 37.9: the shared seed first,
    then the client's CONFIRMED conformance mappings (``resolver``).  Because
    this runs in the read layer, confirming a mapping reclassifies retained rows
    on the next read with no fact rewrite.
    """

    if resolver is not None:
        canonical = resolver(raw_value, connector)
    else:
        canonical = normalize_country_value(raw_value)
    if canonical is None:
        return _UNKNOWN_BUCKET
    market = index.get(canonical)
    if market is not None:
        return _Bucket(market.id, market.label, "tracked", market.country_codes)
    return _OTHER_BUCKET


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
    *,
    resolver: "CountryResolver | None" = None,
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

    index = _market_index(posture)
    groups: dict[tuple[object, ...], list[Mapping[str, object]]] = {}
    bucket_meta: dict[tuple[object, ...], _Bucket] = {}
    dq: list[dict] = []
    identity_fields = ("project_id", "date", "connector", "metric", "pull_id", "loaded_at")

    for row in country_rows:
        raw_value = row.get("breakdown_value")
        connector = row.get("connector")
        bucket = _bucket(raw_value, index, resolver, connector)
        if bucket.kind == "unknown":
            dq.append(
                {
                    "code": "country_value_unmapped",
                    "message": (
                        "Country value could not be mapped to the canonical vocabulary. "
                        "Resolve it once as a governed conformance mapping; the shared "
                        "vocabulary seed is never edited for one client."
                    ),
                    "raw_value": raw_value,
                    "connector": connector,
                    "pull_id": row.get("pull_id"),
                    "canonical_dimension": COUNTRY_PARTITION,
                    "repair_path": "dimension_conformance",
                }
            )
        key = tuple(row.get(field) for field in identity_fields) + (bucket.id,)
        groups.setdefault(key, []).append(row)
        bucket_meta[key] = bucket

    def _sort_key(item: tuple[tuple[object, ...], list[Mapping[str, object]]]) -> tuple[str, ...]:
        key = item[0]
        bucket = str(key[-1])
        bucket_rank = "1" if bucket == OTHER_MARKETS else "2" if bucket == UNKNOWN_MARKET else "0"
        return tuple("" if part is None else str(part) for part in key[:-1]) + (bucket_rank, bucket)

    output: list[dict] = []
    for key, grouped in sorted(groups.items(), key=_sort_key):
        first = dict(grouped[0])
        bucket = bucket_meta[key]
        # Non-additive metrics apply their declared rule AFTER grouping.
        value, rule = _aggregate(grouped, str(first.get("metric") or ""))
        first.update(
            {
                "breakdown_dimension": "market",
                "breakdown_value": bucket.id,
                "value": value,
                "market_id": bucket.id,
                "market_label": bucket.label,
                "market_kind": bucket.kind,
                "market_country_codes": list(bucket.country_codes),
                "market_code": bucket.market_code,
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
    """Accessible descriptors for the default Local-market split.

    Each tracked descriptor carries the stable market id, the operator's label
    and its member country codes.  ``bindable`` marks what a budget or objective
    may bind to: never ``Other markets``, never ``Unknown``.
    """

    if posture.mode != LOCAL_MARKETS:
        return []
    tracked = [
        {
            "id": market.id,
            "label": market.label,
            "kind": "tracked",
            "country_codes": list(market.country_codes),
            "country_code": market.single_country_code,
            "bindable": True,
        }
        for market in posture.markets
    ]
    return tracked + [
        {
            "id": OTHER_MARKETS,
            "label": OTHER_MARKETS_LABEL,
            "kind": "other",
            "country_codes": [],
            "country_code": None,
            "bindable": False,
        },
        {
            "id": UNKNOWN_MARKET,
            "label": UNKNOWN_MARKET_LABEL,
            "kind": "unknown",
            "country_codes": [],
            "country_code": None,
            "bindable": False,
        },
    ]
