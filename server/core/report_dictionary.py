"""toorow — canonical metric/dimension dictionary (Story 6.1).

Single source of truth for validating a report definition's ``metrics[]`` and
``dimensions[]`` against the dbt semantic layer (AC2) and for routing non-additive
metrics to their semantic view (AD-4, AC3).

The dictionary is derived from the dbt seeds so there is ONE authoritative list:
  - ``dbt/seeds/dim_metric.csv`` — the metric dictionary. Columns:
        name, additive, aggregation_rule, ratio_numerator, ratio_denominator
  - ``dbt/seeds/dim_*.csv``       — one seed per dimension. The seed file basename
        ``dim_<name>.csv`` declares the canonical dimension ``<name>`` (e.g.
        ``dim_device.csv`` -> dimension ``device``; ``dim_country.csv`` ->
        ``country``). ``date`` is the implicit time grain dimension and is always
        valid.

# AD-2: source-agnostic — no module-specific strings appear here. The dictionary
#       reflects the warehouse semantic layer, not any single connector.
# AD-4: non-additive metrics (additive == false) carry an ``aggregation_rule``.
#       ``is_non_additive(metric)`` drives the semantic-view routing in
#       warehouse.query_report so ratios / impression-weighted metrics are never
#       naively SUM()'d from fact_daily_kpi.
"""

from __future__ import annotations

import csv
import functools
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo layout: server/core/report_dictionary.py -> repo_root/dbt/seeds
_REPO_ROOT = Path(__file__).parent.parent.parent
_SEEDS_DIR = _REPO_ROOT / "dbt" / "seeds"
_DIM_METRIC_CSV = _SEEDS_DIR / "dim_metric.csv"
# Epic 31: canonical event-type dictionary (the pendant of dim_metric.csv).
_DIM_EVENT_TYPE_CSV = _SEEDS_DIR / "dim_event_type.csv"

# ``date`` is the implicit day-grain dimension present on every fact row; it has
# no dedicated seed file but is always a valid report dimension.
_IMPLICIT_DIMENSIONS = frozenset({"date"})

# Dimension seeds that do not describe a report breakdown dimension. ``dim_metric``
# is the metric dictionary and ``dim_project`` is a warehouse-internal lookup — a
# report never groups by "metric" or "project" as a breakdown.
_NON_DIMENSION_SEEDS = frozenset({"metric", "project", "event_type"})

# Breakdown dimensions declared by loaded modules' manifests (canonical values of
# ``canonical_dimension_mapping``). The dbt dimension seeds only cover the shared
# conformance dimensions (device, country); connector-specific breakdowns such as
# ``campaign_id`` / ``adset_id`` are declared per module. The loader registers
# them here at scan time so report validation recognises the full warehouse
# vocabulary without hard-coding any connector name (AD-2).
_registered_dimensions: set[str] = set()


def register_dimensions(dimensions) -> None:
    """Register additional canonical dimension names (called by the loader).

    Idempotent; lowercases and dedups. Used to add connector-declared breakdown
    dimensions (from manifest.canonical_dimension_mapping) to the report
    dimension vocabulary.
    """
    for dim in dimensions or []:
        name = str(dim).strip().lower()
        if name:
            _registered_dimensions.add(name)


@functools.lru_cache(maxsize=1)
def _load_metric_dictionary() -> dict[str, dict]:
    """Load dim_metric.csv into {metric_name: {additive, aggregation_rule, ...}}.

    Cached — the seed is static at runtime. Returns an empty dict (and logs a
    warning) if the seed is missing, so callers degrade gracefully rather than
    crashing module load.
    """
    result: dict[str, dict] = {}
    if not _DIM_METRIC_CSV.is_file():
        logger.warning(
            "report_dictionary: dim_metric.csv not found at %s — metric validation disabled",
            _DIM_METRIC_CSV,
        )
        return result
    with _DIM_METRIC_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip().lower()
            if not name:
                continue
            additive_raw = (row.get("additive") or "").strip().lower()
            result[name] = {
                "additive": additive_raw == "true",
                "aggregation_rule": (row.get("aggregation_rule") or "").strip(),
                "ratio_numerator": (row.get("ratio_numerator") or "").strip() or None,
                "ratio_denominator": (row.get("ratio_denominator") or "").strip() or None,
            }
    return result


@functools.lru_cache(maxsize=1)
def _load_dimension_dictionary() -> frozenset[str]:
    """Return the set of canonical dimension names from the dbt dimension seeds.

    A ``dim_<name>.csv`` seed declares dimension ``<name>``; ``date`` is added as
    the implicit time grain. ``dim_metric`` / ``dim_project`` are excluded (they
    are not report breakdown dimensions).
    """
    dims: set[str] = set(_IMPLICIT_DIMENSIONS)
    if not _SEEDS_DIR.is_dir():
        logger.warning(
            "report_dictionary: dbt seeds dir not found at %s — dimension validation limited",
            _SEEDS_DIR,
        )
        return frozenset(dims)
    for seed in _SEEDS_DIR.glob("dim_*.csv"):
        name = seed.stem[len("dim_") :].strip().lower()
        if name and name not in _NON_DIMENSION_SEEDS:
            dims.add(name)
    return frozenset(dims)


@functools.lru_cache(maxsize=1)
def _load_event_type_dictionary() -> dict[str, dict]:
    """Load dim_event_type.csv into {event_type: {category, default_marker, ...}}.

    Epic 31: the strict pendant of ``_load_metric_dictionary``. Cached; returns an
    empty dict (and logs a warning) if the seed is missing so callers degrade
    gracefully rather than crashing module load.
    """
    result: dict[str, dict] = {}
    if not _DIM_EVENT_TYPE_CSV.is_file():
        logger.warning(
            "report_dictionary: dim_event_type.csv not found at %s — event validation disabled",
            _DIM_EVENT_TYPE_CSV,
        )
        return result
    with _DIM_EVENT_TYPE_CSV.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("event_type") or "").strip().lower()
            if not name:
                continue
            result[name] = {
                "category": (row.get("category") or "").strip(),
                "default_marker": (row.get("default_marker") or "").strip(),
                "description": (row.get("description") or "").strip(),
            }
    return result


def valid_metrics() -> frozenset[str]:
    """Return the set of canonical metric names (from dim_metric.csv)."""
    return frozenset(_load_metric_dictionary().keys())


def valid_event_types() -> frozenset[str]:
    """Return the set of canonical event types (from dim_event_type.csv). Epic 31."""
    return frozenset(_load_event_type_dictionary().keys())


def valid_dimensions() -> frozenset[str]:
    """Return canonical dimension names: dim_*.csv seeds + 'date' + module-declared."""
    return _load_dimension_dictionary() | frozenset(_registered_dimensions)


def is_known_metric(metric: str) -> bool:
    """True if ``metric`` exists in the canonical metric dictionary."""
    return metric.strip().lower() in _load_metric_dictionary()


def is_known_dimension(dimension: str) -> bool:
    """True if ``dimension`` exists in the canonical dimension dictionary."""
    name = dimension.strip().lower()
    return name in _load_dimension_dictionary() or name in _registered_dimensions


def is_known_event_type(event_type: str) -> bool:
    """True if ``event_type`` exists in the canonical event dictionary (Epic 31).

    Strict pendant of ``is_known_metric``: an unknown event_type is a drift signal
    (the caller refuses), never a silent drop.
    """
    return event_type.strip().lower() in _load_event_type_dictionary()


def is_non_additive(metric: str) -> bool:
    """True if ``metric`` is declared non-additive (has an aggregation_rule) — AD-4.

    Non-additive metrics (ratios like cpa/ctr/roas, impression-weighted
    average_position) MUST be routed to their semantic view, never SUM()'d from
    fact_daily_kpi. Unknown metrics return False (treated as additive; the loader
    rejects unknown metrics before they reach a query anyway).
    """
    entry = _load_metric_dictionary().get(metric.strip().lower())
    if entry is None:
        return False
    return not entry["additive"]


def enrich_events_with_dim(events: list[dict]) -> list[dict]:
    """Join dim_event_type fields (category, default_marker) onto context event dicts.

    Epic 31 (Story 31.5): the widget overlay and the get_events MCP tool both need
    category and default_marker to drive marker style and category filtering.
    This is a pure in-memory join against the cached dim_event_type dictionary —
    no I/O. Unknown event_type values receive category='' and default_marker='pin'
    (fallback pin marker, same visual as 'milestone').

    AD-2: source-agnostic; no module-specific strings.
    AD-9: overlay is additive — this function never modifies metric values.
    """
    dim = _load_event_type_dictionary()
    enriched: list[dict] = []
    for evt in events:
        et = (evt.get("type") or "").strip().lower()
        entry = dim.get(et, {})
        enriched.append(
            {
                **evt,
                "category": entry.get("category", ""),
                "default_marker": entry.get("default_marker", "pin"),
            }
        )
    return enriched


def aggregation_rule(metric: str) -> str | None:
    """Return the declared aggregation_rule for ``metric`` (None if unknown)."""
    entry = _load_metric_dictionary().get(metric.strip().lower())
    return entry["aggregation_rule"] if entry else None
