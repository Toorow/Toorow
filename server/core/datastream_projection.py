"""Safe KPI projection compiler (Story 12.4) — compile-time gates ONLY.

This module is the SOLE owner of the pure, source-agnostic safe-projection
compiler. It turns ONE immutable Story 12.3 mapping version into two governed
outputs that keep a single truth:

  1. a FULL-GRAIN relation spec that preserves EVERY selected dimension at the
     declared joint grain (every dimension as its own typed column + a
     deterministic ``grain_key`` + all source fields + full provenance); and
  2. a SAFE canonical projection into ``fact_daily_kpi`` that accepts ONLY
     measures the mapping declares additive (``aggregation='sum'`` AND
     ``non_additive=false`` via ``app.mdm_canonical_fields``) plus EXACTLY ONE
     explicitly governed dimension projection into the canonical
     ``breakdown_dimension``/``breakdown_value`` slot.

Everything else FAILS CLOSED at compile time with a stable
``{code, path, field_ids, repair}`` issue drawn from a CLOSED set. A rejected
non-additive measure is NEVER summed into ``fact_daily_kpi``; it stays in the
full-grain relation and is served by the ``semantic_*`` VIEW pattern (a
``route_to_semantic`` repair).

Scope boundary (12.4): COMPILE-TIME gates + relation shape only. It writes no
mart (AD-8: dbt is the only mart writer), moves no published pointer, calls no
provider, and performs no live BigQuery dry-run (AI-13 N/A). Publication
atomicity/active-pointer is 12.5; classification/mapping is 12.3; dispatch is
12.6.

Determinism: the same immutable mapping version yields a byte-identical plan.
No provider name, provider field name, or connector branch appears here (AD-2):
additivity/dimension classification arrives EXCLUSIVELY through the mapping's
``aggregation``/``non_additive`` flags (resolved through the MDM registry by
12.3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

_PROJECTION_SCHEMA_PATH = (
    Path(__file__).parent / "schemas" / "datastream-projection.schema.json"
)
_PROJECTION_CONTRACT_VERSION = "1"

# Governed cardinality/scan threshold defaults (Open Question 3). Used ONLY when
# app.project_preferences supplies no per-project override — never a silent
# platform-wide hardcode: the plan records threshold_source='documented_default'
# so a consumer can see the default was used ("prefer per-project preferences").
DEFAULT_MAX_GRAIN_CARDINALITY = 1_000_000  # distinct-grain rows
DEFAULT_MAX_SCAN_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB

# The closed set of rejection codes (mirrors the schema enum). A projection with
# ANY issue is not executable and blocks publication (12.5).
REJECT_NON_ADDITIVE_MEASURE = "non_additive_measure_projected"
REJECT_UNGOVERNED_DIMENSION = "ungoverned_dimension_projection"
REJECT_GOVERNED_DIM_SHADOWS_CANONICAL = "governed_dim_shadows_canonical"
REJECT_MIXED_GRAIN = "mixed_grain_projection"
REJECT_CARDINALITY_OVER_LIMIT = "cardinality_over_limit"
REJECT_SCAN_OVER_LIMIT = "scan_over_limit"
REJECT_MAPPING_NOT_EXECUTABLE = "mapping_not_executable"
REJECT_MAPPING_DRIFT = "mapping_drift"

# Physical-type -> full-grain typed-column class + a nominal per-value byte width
# used for the metadata scan estimate (no live BigQuery). Warehouse vocabulary
# only (AD-2), NOT a provider/module string.
_NUMERIC_PHYSICAL_TYPES = frozenset(
    {
        "integer",
        "decimal",
        "float",
        "number",
        "bigint",
        "double precision",
        "numeric",
        "int8",
        "int4",
        "int2",
        "float64",
        "real",
        "smallint",
        "currency",
    }
)
_DATE_PHYSICAL_TYPES = frozenset({"date", "timestamp", "datetime", "timestamptz"})

# Nominal byte widths per value for the metadata scan estimate. Deliberately
# coarse — this is a governed pre-flight estimate, not a live dry_run byte count.
_WIDTH_NUMERIC = 8
_WIDTH_DATE = 8
_WIDTH_STRING = 32

# Nominal distinct-value multipliers per cardinality_signal (12.3 profile). The
# joint-grain cardinality is estimated as the PRODUCT of the per-dimension
# distinct-value estimates (an upper-bound-style product, honest about the risk
# a high-cardinality dimension explodes the grain).
_CARDINALITY_SIGNAL_DISTINCT = {
    "unique": 100_000,
    "high": 10_000,
    "medium": 500,
    "low": 20,
    "unknown": 1_000,
}
# A dimension whose per-value distinct estimate is at/above this is flagged as an
# "expensive field" driving an over-limit estimate (named, never silently capped).
_EXPENSIVE_DISTINCT_THRESHOLD = 10_000


class ProjectionCompileError(ValueError):
    """The projection plan failed strict-schema validation after compilation.

    This is an INTERNAL invariant breach (the compiler produced a
    schema-invalid plan), distinct from a fail-closed rejection issue which is
    carried in ``plan['issues']`` and returned normally.
    """


def _projection_schema_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_PROJECTION_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _type_class(physical_type: str) -> str:
    pt = (physical_type or "").strip().lower()
    if pt in _DATE_PHYSICAL_TYPES:
        return "date"
    if pt in _NUMERIC_PHYSICAL_TYPES:
        return "numeric"
    return "string"


def _value_width(type_class: str) -> int:
    if type_class == "numeric":
        return _WIDTH_NUMERIC
    if type_class == "date":
        return _WIDTH_DATE
    return _WIDTH_STRING


def _is_additive(field: dict[str, Any]) -> bool:
    """A measure is additive iff aggregation='sum' AND non_additive=false.

    Reads ONLY the mapping's declared flags (which 12.3 resolves through
    app.mdm_canonical_fields). No provider heuristic (AD-2).
    """
    sugg = field.get("suggestion") or {}
    aggregation = str(sugg.get("aggregation") or "none").strip().lower()
    non_additive = bool(sugg.get("non_additive", False))
    return aggregation == "sum" and not non_additive


def _is_measure(field: dict[str, Any]) -> bool:
    sugg = field.get("suggestion") or {}
    return str(sugg.get("semantic_role") or "").startswith("measure")


def _field_grain_class(field: dict[str, Any]) -> str:
    """Grain-identity class of a field: 'date' (day) vs 'instant' vs 'dimension'.

    Used by the mixed_grain gate: two date-class grain fields at differing
    granularity (a 'day' date column alongside a 'timestamp' instant column)
    cannot share one joint grain without an explicit downcast — the same
    discipline the 12.3 profiler enforces upstream, re-checked here so a drifted
    or hand-built mapping still fails closed.
    """
    physical_type = str(field.get("physical_type") or "").strip().lower()
    if physical_type == "date":
        return "day"
    if physical_type in {"timestamp", "datetime", "timestamptz"}:
        return "instant"
    return "dimension"


def _shadows_canonical(
    projected_breakdown: str, connector_canonical_breakdown: str | None
) -> bool:
    """True iff a governed projected dimension would re-pin the connector's canonical.

    ``rollup.canonical_breakdown_per_connector`` (and the dbt marts'
    ``MIN(breakdown_dimension)``) select the canonical partition by
    (day-coverage desc, name asc) -- i.e. the LEXICOGRAPHICALLY SMALLEST name
    among the max-day-coverage partitions. A 12.4 governed projection lands as a
    PARALLEL series that fully covers each day (it totals the day like every
    other breakdown), so it ties the current canonical on day-coverage and the
    lexicographic MIN decides. If the projected ``breakdown_dimension`` name sorts
    AT or BEFORE the connector's current canonical, the selector would re-pin it
    as canonical and the additive hero KPI total would shift/inflate by the
    number of parallel partitions (the Epic-10 xN failure mode).

    Fail closed: when the connector's canonical is UNKNOWN (None) we cannot prove
    the projected series stays parallel, so we treat it as shadowing and reject.
    Only a projection provably sorting STRICTLY AFTER a KNOWN canonical is safe.
    """
    if connector_canonical_breakdown is None:
        # Canonical unknown at compile time -> cannot prove the series is parallel.
        return True
    # <= : equal name collides with the same slot; a name sorting before it wins
    # the lexicographic-MIN tie and re-pins canonical. Both are rejected.
    return projected_breakdown <= str(connector_canonical_breakdown)


def resolve_thresholds(
    preferences: dict[str, Any] | None,
) -> tuple[int, int, str]:
    """Resolve the governed cardinality/scan thresholds for a project.

    Reads ``max_projection_grain_cardinality`` / ``max_projection_scan_bytes``
    from the project-scoped ``app.project_preferences`` row (passed in as a plain
    dict by the caller). Falls back to the DOCUMENTED defaults when unset —
    recording ``threshold_source`` so no platform-wide hardcode is applied
    silently ("prefer per-project preferences").

    Returns ``(max_grain_cardinality, max_scan_bytes, threshold_source)``.
    """
    prefs = preferences or {}
    card = prefs.get("max_projection_grain_cardinality")
    scan = prefs.get("max_projection_scan_bytes")
    source = "documented_default"
    if card is not None or scan is not None:
        source = "project_preference"
    max_card = int(card) if card is not None else DEFAULT_MAX_GRAIN_CARDINALITY
    max_scan = int(scan) if scan is not None else DEFAULT_MAX_SCAN_BYTES
    if max_card < 1:
        max_card = DEFAULT_MAX_GRAIN_CARDINALITY
    if max_scan < 1:
        max_scan = DEFAULT_MAX_SCAN_BYTES
    return max_card, max_scan, source


def estimate_cardinality_and_scan(
    mapping_payload: dict[str, Any],
    *,
    max_grain_cardinality: int,
    max_scan_bytes: int,
    threshold_source: str,
    approved: bool = False,
) -> dict[str, Any]:
    """Metadata-based cardinality/scan estimate (no live BigQuery dry-run).

    Cardinality is the PRODUCT of per-grain-dimension distinct-value estimates
    read from each field's 12.3 ``cardinality_signal``. Scan bytes = estimated
    grain rows x (sum of typed-column widths across grain columns + selected
    source fields). Over-limit NAMES the concrete expensive field(s) driving it;
    it never silently drops or top-N-caps a dimension (AD-9).
    """
    grain = list(mapping_payload.get("grain") or [])
    grain_set = set(grain)
    fields_by_id = {
        f.get("field_id"): f for f in mapping_payload.get("fields") or []
    }

    est_cardinality = 1
    row_width = 0
    expensive_fields: list[str] = []

    for field_id in grain:
        field = fields_by_id.get(field_id) or {}
        prof = field.get("profile") or {}
        signal = str(prof.get("cardinality_signal") or "unknown").strip().lower()
        distinct = _CARDINALITY_SIGNAL_DISTINCT.get(signal, _CARDINALITY_SIGNAL_DISTINCT["unknown"])
        est_cardinality *= max(1, distinct)
        row_width += _value_width(_type_class(str(field.get("physical_type") or "")))
        if distinct >= _EXPENSIVE_DISTINCT_THRESHOLD:
            expensive_fields.append(field_id)

    # Selected source fields (measures kept in the full-grain relation) widen the
    # scanned row but do not multiply the grain cardinality.
    for field in mapping_payload.get("fields") or []:
        fid = field.get("field_id")
        if fid in grain_set:
            continue
        row_width += _value_width(_type_class(str(field.get("physical_type") or "")))

    est_scan_bytes = est_cardinality * max(1, row_width)

    over_card = est_cardinality > max_grain_cardinality
    over_scan = est_scan_bytes > max_scan_bytes

    return {
        "estimated_grain_cardinality": int(est_cardinality),
        "estimated_scan_bytes": int(est_scan_bytes),
        "max_grain_cardinality": int(max_grain_cardinality),
        "max_scan_bytes": int(max_scan_bytes),
        "over_limit": bool(over_card or over_scan),
        "expensive_fields": sorted(set(expensive_fields)),
        "threshold_source": threshold_source,
        "approved": bool(approved),
    }


def _build_full_grain_relation(mapping_payload: dict[str, Any]) -> dict[str, Any]:
    """Full-grain relation spec: every selected dimension as its own typed column.

    Never a long-format breakdown_dimension/breakdown_value pair. Carries an
    explicit grain_key (AI-05 uniqueness) + all source fields + full provenance.
    """
    grain = list(mapping_payload.get("grain") or [])
    grain_set = set(grain)
    fields_by_id = {
        f.get("field_id"): f for f in mapping_payload.get("fields") or []
    }

    grain_columns = [
        {
            "field_id": field_id,
            "type_class": _type_class(
                str((fields_by_id.get(field_id) or {}).get("physical_type") or "")
            ),
        }
        for field_id in grain
    ]

    # Every selected source field NOT already in the grain is kept as its own
    # typed column (measures preserved at full grain, not aggregated away).
    source_fields = [
        f.get("field_id")
        for f in mapping_payload.get("fields") or []
        if f.get("field_id") not in grain_set
        and (f.get("binding") or {}).get("status") != "excluded"
    ]

    return {
        "grain_columns": grain_columns,
        "grain_key": "grain_key",
        "source_fields": sorted(str(s) for s in source_fields if s),
        "provenance": {
            "execution_id": "execution_id",
            "pull_id": "pull_id",
            "mapping_version_id": "mapping_version_id",
            "plan_version_id": "plan_version_id",
            "loaded_at": "loaded_at",
        },
        "scope_columns": ["connector", "project_id"],
    }


def compile_projection(
    mapping_version: dict[str, Any],
    *,
    project_preferences: dict[str, Any] | None = None,
    dimension_projection: str | None = None,
    connector_canonical_breakdown: str | None = None,
    approved: bool = False,
) -> dict[str, Any]:
    """Compile a safe KPI projection plan from ONE immutable 12.3 mapping version.

    Args:
        mapping_version: an immutable 12.3 mapping version dict (as returned by
            ``datastream_field_mapping.get_mapping_version``), carrying ``id``,
            ``plan_version_id``, ``source_schema_hash``, ``capability_fingerprint``,
            ``executable``, and ``mapping_payload``. It is the authoritative,
            content-hashed additivity/grain source.
        project_preferences: the project-scoped ``app.project_preferences`` row as
            a dict (or None). Supplies the governed cardinality/scan thresholds;
            unset keys fall back to the documented defaults.
        dimension_projection: the ONE governed source ``field_id`` the operator
            elects to project into the canonical ``breakdown_dimension`` slot, or
            None for a pure additive projection with no breakdown series. A
            second/arbitrary dimension is rejected (``ungoverned_dimension_projection``).
        connector_canonical_breakdown: the source's CURRENT canonical
            ``breakdown_dimension`` name (the partition ``rollup.canonical_breakdown_per_connector``
            / the dbt marts' ``MIN(breakdown_dimension)`` already pin for this
            source; supplied by the caller, never hardcoded here). Required when
            ``dimension_projection`` is set: the projected breakdown MUST sort
            STRICTLY AFTER it so the selector never re-pins the projected series
            as canonical (which would shift/inflate the additive hero KPI total --
            the Epic-10 xN failure mode). A projected breakdown sorting at/before
            the canonical, or an UNKNOWN canonical, is rejected
            (``governed_dim_shadows_canonical``) -- fail closed. When
            ``dimension_projection`` is None this is unused.
        approved: the explicit governed approval flag for an over-limit projection.

    Returns a projection plan dict validated against the strict schema. A plan
    with a non-empty ``issues`` list is NOT executable and blocks publication;
    nothing is silently discarded or coerced. Deterministic: identical input
    yields a byte-identical plan.
    """
    payload = mapping_version.get("mapping_payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    fields = list(payload.get("fields") or [])
    fields_by_id = {f.get("field_id"): f for f in fields}
    grain = list(payload.get("grain") or [])

    issues: list[dict[str, Any]] = []

    # --- Gate 0: the mapping itself must be executable and un-drifted. ------
    # A non-executable 12.3 mapping (blocking bindings) or a drifted one
    # (source_schema_hash / capability_fingerprint no longer matches) can never
    # be safely projected. Fail closed BEFORE inspecting individual fields.
    if not mapping_version.get("executable", False):
        issues.append(
            {
                "code": REJECT_MAPPING_NOT_EXECUTABLE,
                "path": "$.mapping_version",
                "field_ids": [],
                "repair": {"resolve_blocking_bindings": "confirm_or_exclude"},
            }
        )

    drift = mapping_version.get("drift")
    if drift and (
        drift.get("source_schema_hash_changed") or drift.get("capability_changed")
    ):
        issues.append(
            {
                "code": REJECT_MAPPING_DRIFT,
                "path": "$.mapping_version",
                "field_ids": [],
                "repair": {"re_version_mapping": "reclassify_against_current_schema"},
            }
        )

    # --- Gate 1: additive-measure acceptance (NEVER sum a non-additive). ----
    additive_measures: list[dict[str, Any]] = []
    for field in fields:
        if not _is_measure(field):
            continue
        binding = field.get("binding") or {}
        if binding.get("status") == "excluded":
            continue
        field_id = field.get("field_id")
        if _is_additive(field):
            metric_name = binding.get("canonical_target") or field_id
            additive_measures.append(
                {
                    "field_id": field_id,
                    "metric": str(metric_name),
                    "mdm_target": binding.get("mdm_target"),
                    "aggregation": "sum",
                    "non_additive": False,
                }
            )
        else:
            # A non-additive measure proposed for additive projection is
            # REJECTED and ROUTED to the semantic_* VIEW pattern — never summed
            # into fact_daily_kpi (AD-4). It stays in the full-grain relation.
            issues.append(
                {
                    "code": REJECT_NON_ADDITIVE_MEASURE,
                    "path": f"$.fields[?(@.field_id=='{field_id}')]",
                    "field_ids": [field_id],
                    "repair": {"route_to_semantic": "semantic_view"},
                }
            )

    # --- Gate 2: exactly ONE governed dimension projection. -----------------
    dimension_field_ids = [
        f.get("field_id")
        for f in fields
        if not _is_measure(f)
        and _field_grain_class(f) == "dimension"
        and (f.get("binding") or {}).get("status") != "excluded"
    ]

    governed_projection: dict[str, Any] | None = None
    if dimension_projection is not None:
        chosen = fields_by_id.get(dimension_projection)
        if (
            chosen is None
            or _is_measure(chosen)
            or _field_grain_class(chosen) != "dimension"
            or dimension_projection not in dimension_field_ids
        ):
            # The elected projection is not a governed source dimension of this
            # mapping — squeezing an arbitrary/second dimension into the single
            # canonical slot is rejected.
            issues.append(
                {
                    "code": REJECT_UNGOVERNED_DIMENSION,
                    "path": "$.governed_dimension_projection",
                    "field_ids": [dimension_projection],
                    "repair": {"choose_governed_dimension": sorted(dimension_field_ids)},
                }
            )
        else:
            binding = chosen.get("binding") or {}
            projected_breakdown = str(
                binding.get("canonical_target") or dimension_projection
            )
            # A governed projection is a PARALLEL series -- it must sort so
            # canonical_breakdown_per_connector / MIN(breakdown_dimension) never
            # re-pins it as the connector's canonical partition. A breakdown name
            # sorting at/before the current canonical (or an unknown canonical)
            # would shift/inflate the additive hero KPI total (Epic-10 xN). Reject
            # and fail closed BEFORE emitting it as a governed projection.
            if _shadows_canonical(
                projected_breakdown, connector_canonical_breakdown
            ):
                issues.append(
                    {
                        "code": REJECT_GOVERNED_DIM_SHADOWS_CANONICAL,
                        "path": "$.governed_dimension_projection",
                        "field_ids": [dimension_projection],
                        "repair": {
                            "choose_breakdown_sorting_after_canonical": {
                                "projected_breakdown": projected_breakdown,
                                "connector_canonical_breakdown": (
                                    connector_canonical_breakdown
                                ),
                            }
                        },
                    }
                )
            else:
                governed_projection = {
                    "field_id": dimension_projection,
                    "breakdown_dimension": projected_breakdown,
                    "mdm_target": binding.get("mdm_target"),
                }

    # --- Gate 3: mixed-grain guard (DATE-granularity bleed ONLY). -----------
    # SCOPE: this gate detects only DIFFERING DATE-CLASS granularities in the
    # grain -- a 'day' date column folded together with a 'timestamp' instant
    # column, which would sum two date grains into one series. It does NOT (and
    # cannot, from mapping metadata alone) detect ENTITY-LEVEL grain bleed -- e.g.
    # a campaign_id carried on adgroup/creative-grain rows (the tiktok/meta
    # 15.2/15.9 F-1 scenario the story's "Hard Part" cites). Entity-level grain
    # bleed is structurally prevented DOWNSTREAM by the candidate_full_grain
    # grain_key UNIQUE constraint (candidate_full_grain_grain_unique, AI-05): two
    # rows sharing a joint-grain identity cannot coexist, so a lower-grain row
    # carrying a higher-grain id cannot double-count. Do not claim this compiler
    # gate guards entity-level bleed -- it guards date-granularity bleed only.
    grain_date_classes = {
        _field_grain_class(fields_by_id.get(fid) or {})
        for fid in grain
        if _field_grain_class(fields_by_id.get(fid) or {}) in {"day", "instant"}
    }
    if len(grain_date_classes) > 1:
        mixed_ids = sorted(
            fid
            for fid in grain
            if _field_grain_class(fields_by_id.get(fid) or {}) in {"day", "instant"}
        )
        issues.append(
            {
                "code": REJECT_MIXED_GRAIN,
                "path": "$.grain",
                "field_ids": mixed_ids,
                "repair": {"align_grain": "choose_single_granularity"},
            }
        )

    # --- Gate 4: governed cardinality/scan estimate. ------------------------
    max_card, max_scan, threshold_source = resolve_thresholds(project_preferences)
    estimate = estimate_cardinality_and_scan(
        payload,
        max_grain_cardinality=max_card,
        max_scan_bytes=max_scan,
        threshold_source=threshold_source,
        approved=approved,
    )
    if estimate["over_limit"] and not approved:
        if estimate["estimated_grain_cardinality"] > estimate["max_grain_cardinality"]:
            issues.append(
                {
                    "code": REJECT_CARDINALITY_OVER_LIMIT,
                    "path": "$.estimate",
                    "field_ids": estimate["expensive_fields"],
                    "repair": {
                        "approve_or_reduce_grain": {
                            "estimated_grain_cardinality": estimate[
                                "estimated_grain_cardinality"
                            ],
                            "max_grain_cardinality": estimate["max_grain_cardinality"],
                        }
                    },
                }
            )
        if estimate["estimated_scan_bytes"] > estimate["max_scan_bytes"]:
            issues.append(
                {
                    "code": REJECT_SCAN_OVER_LIMIT,
                    "path": "$.estimate",
                    "field_ids": estimate["expensive_fields"],
                    "repair": {
                        "approve_or_reduce_scan": {
                            "estimated_scan_bytes": estimate["estimated_scan_bytes"],
                            "max_scan_bytes": estimate["max_scan_bytes"],
                        }
                    },
                }
            )

    # Deterministic issue ordering (stable across runs / identical input).
    issues.sort(key=lambda item: (item["code"], item["path"], tuple(item["field_ids"])))

    plan: dict[str, Any] = {
        "projection_contract_version": _PROJECTION_CONTRACT_VERSION,
        "mapping_version_id": str(mapping_version.get("id") or ""),
        "plan_version_id": str(
            mapping_version.get("plan_version_id") or payload.get("plan_version_id") or ""
        ),
        "source_schema_hash": str(
            mapping_version.get("source_schema_hash")
            or payload.get("source_schema_hash")
            or ""
        ),
        "capability_fingerprint": mapping_version.get("capability_fingerprint")
        or payload.get("capability_fingerprint"),
        "executable": not issues,
        "grain": grain,
        "full_grain_relation": _build_full_grain_relation(payload),
        "additive_measures": sorted(additive_measures, key=lambda m: m["field_id"]),
        "governed_dimension_projection": governed_projection,
        "estimate": estimate,
        "issues": issues,
    }

    validator = _projection_schema_validator()
    errors = sorted(
        validator.iter_errors(plan),
        key=lambda err: (list(err.absolute_path), err.message),
    )
    if errors:
        raise ProjectionCompileError(
            f"compiled projection plan failed schema validation: {errors[0].message}"
        )

    return plan
