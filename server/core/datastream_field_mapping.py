"""Immutable Datastream field mapping and Ossie 0.1.1 projection service (Story 12.3).

This module is source-agnostic (AD-2). Field universes and capabilities arrive
exclusively via the Story 12.1 normalized catalog or explicit inputs.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
from ulid import ULID

from core.audit import declare_action
from core.context_event_import import (
    event_roles,
    validate_event_declaration,
)
from core.object_kind_registry import (
    REASON_LOOKUP_FAILED,
    EntityDesignationIssue,
    entity_designations,
    fetch_entity_type_lookup,
    validate_entity_designations,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_DATASTREAM_MAPPING_VERSIONED = declare_action("datastream.mapping.versioned")


_MAPPING_SCHEMA_PATH = Path(__file__).parent / "schemas" / "datastream-field-mapping.schema.json"
_OSSIE_SCHEMA_PATH = Path(__file__).parent / "schemas" / "ossie-0.1.1-profile.schema.json"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.75

# Ambiguity/conflict codes. Currency and non-additive-additive conflicts reuse the
# EXACT constants recognized by datamodel._detect_conflicts so the two layers speak
# one vocabulary (Story 12.3 records conflict state; Epic 13 resolves it).
CONFLICT_CURRENCY = "CURRENCY_CONFLICT"  # datamodel._detect_conflicts
CONFLICT_MEASURE_NULL = "MEASURE_NULL"  # datamodel._detect_conflicts (non-additive/aggregation)

# Recognized physical types. A field whose physical_type/kind is outside this
# universe is an `unknown_field` ambiguity, not a silently-typed dimension.
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
_STRING_PHYSICAL_TYPES = frozenset(
    {"string", "text", "varchar", "char", "uuid", "boolean", "bool", "json", "jsonb"}
)
_KNOWN_KINDS = frozenset({"metric", "dimension", "date"})

# Date granularity buckets used by the mixed_grain detector.
_DAY_DATE_TYPES = frozenset({"date"})
_INSTANT_DATE_TYPES = frozenset({"timestamp", "datetime", "timestamptz"})


class DatastreamMappingStructuralError(ValueError):
    """Raised when a mapping payload is structurally invalid."""

    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{item.get('path')}: {item.get('message')}" for item in issues))


class DatastreamMappingNotFound(LookupError):
    """The project-scoped Datastream or mapping version does not exist."""


class DatastreamMappingConflict(ValueError):
    """An idempotency key was reused with a different payload."""


class DatastreamMappingUnavailable(RuntimeError):
    """The mapping could not be validated or saved safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mint_mapping_id() -> str:
    return f"dmap_{ULID()}"


def _json_path(parts: Any) -> str:
    if not parts:
        return "$"
    res = "$"
    for part in parts:
        sp = str(part)
        if sp.isdigit():
            res += f"[{sp}]"
        else:
            res += f".{sp}"
    return res


def _mapping_schema_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_MAPPING_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _ossie_schema_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_OSSIE_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def compute_source_schema_hash(fields: list[dict[str, Any]]) -> str:
    """Compute a deterministic hash of the ordered field universe."""
    minimal = []
    for field in fields:
        field_id = field.get("field_id") or field.get("name") or field.get("id")
        physical_type = field.get("physical_type") or field.get("data_type") or "unknown"
        kind = field.get("kind") or "dimension"
        minimal.append(
            {
                "field_id": str(field_id),
                "physical_type": str(physical_type),
                "kind": str(kind),
            }
        )
    sorted_minimal = sorted(minimal, key=lambda item: item["field_id"])
    return _sha256(_canonical_json(sorted_minimal))


def physical_type_class(physical_type: str, kind: str) -> str:
    """Classify a physical type into numeric/date/string/unknown.

    Recognition is by explicit type or kind; anything else is `unknown` so the
    caller can surface an `unknown_field` ambiguity rather than guess.
    """
    pt = (physical_type or "").strip().lower()
    kd = (kind or "").strip().lower()
    if pt in _DATE_PHYSICAL_TYPES or kd == "date":
        return "date"
    if pt in _NUMERIC_PHYSICAL_TYPES or kd == "metric":
        return "numeric"
    if pt in _STRING_PHYSICAL_TYPES:
        return "string"
    if kd == "dimension":
        # A dimension with a genuinely unknown physical type is still unknown.
        return "string" if pt else "unknown"
    return "unknown"


#: `mdm_common_keys` compares the physical types implementing one common-key
#: component against each other. It reads THIS classifier rather than typing a
#: second vocabulary that would drift from this one.
_physical_type_class = physical_type_class


def date_granularity(physical_type: str) -> str:
    """Return the date granularity bucket for a date field."""
    pt = (physical_type or "").strip().lower()
    if pt in _INSTANT_DATE_TYPES:
        return "instant"
    if pt in _DAY_DATE_TYPES:
        return "day"
    return "unspecified"


#: `multi_source_plan` compares the date granularity of the two columns
#: implementing one common-key component. It reads THIS function rather than
#: typing a second table of date types that would drift from this one.
_date_granularity = date_granularity


def _derive_confidence(
    *,
    type_class: str,
    physical_type: str,
    kind: str,
    semantic_hints: list[str],
    sample_count: int,
    non_null_count: int,
) -> float:
    """Derive a confidence score from profiling signals.

    Signals (each additive, clamped to [0, 1]):
      - physical-type certainty: a recognized type/kind is worth more than unknown.
      - kind alignment: an explicit metric/dimension/date kind adds certainty.
      - semantic-hint presence: an upstream hint corroborates the role.
      - sample/evidence sufficiency: more non-null observed rows raise confidence.
    An unknown physical type is capped low so it stays blocking by default.
    """
    if type_class == "unknown":
        return 0.30

    score = 0.42
    if (physical_type or "").strip().lower():
        score += 0.20
    if (kind or "").strip().lower() in _KNOWN_KINDS:
        score += 0.20
    if semantic_hints:
        score += 0.08

    # Evidence sufficiency from the bounded sample.
    if sample_count <= 0:
        # No sample provided: metadata-only, cannot corroborate — no bonus.
        score += 0.0
    elif non_null_count >= 3:
        score += 0.07
    elif non_null_count >= 1:
        score += 0.03
    else:
        # Sample provided but the column was entirely null: weak evidence.
        score -= 0.25

    return max(0.0, min(1.0, round(score, 4)))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def normalize_mapping(mapping: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate structural schema compliance and compute canonical content hash."""
    if not isinstance(mapping, dict):
        raise DatastreamMappingStructuralError(
            [{"code": "invalid_schema", "path": "$", "message": "Mapping must be an object."}]
        )

    normalized = _normalize_value(deepcopy(mapping))
    issues = []
    validator = _mapping_schema_validator()
    for error in sorted(
        validator.iter_errors(normalized),
        key=lambda err: (_json_path(err.absolute_path), err.message),
    ):
        issues.append(
            {
                "code": "invalid_schema",
                "path": _json_path(error.absolute_path),
                "message": error.message,
            }
        )

    if issues:
        raise DatastreamMappingStructuralError(issues)

    content_hash = _sha256(_canonical_json(normalized))
    return normalized, content_hash


#: The aggregations `datastream-field-mapping.schema.json` accepts on a
#: suggestion. Connector manifests describe the SOURCE's own aggregation, whose
#: vocabulary is deliberately wider, and the two sets are not the same contract.
_MAPPING_AGGREGATIONS = frozenset({"none", "sum", "avg", "min", "max", "count", "custom"})


def _mapping_aggregation(source_aggregation: str) -> tuple[str, str | None]:
    """Translate a source aggregation into the mapping vocabulary, losing nothing.

    Measured across the 38 connector manifests on 2026-07-30, 14 of 453 declared
    fields aggregate as `impression_weighted` (10) or `latest` (4) -- both real
    source semantics, neither in the mapping enum. Passing them through made
    `normalize_mapping` reject the whole mapping on a schema violation, so every
    report profile containing one of those fields was unmappable.

    `custom` is the escape the schema already declares for exactly this. The
    source term is kept in `evidence` rather than dropped: `custom` alone would
    say "not one of the standard six" without ever saying which, and the
    non-additivity that makes `impression_weighted` matter is carried separately
    by `non_additive`.
    """
    aggregation = str(source_aggregation or "none")
    if aggregation in _MAPPING_AGGREGATIONS:
        return aggregation, None
    return "custom", f"source_aggregation:{aggregation}"


def profile_fields(
    *,
    field_records: list[dict[str, Any]],
    sample_data: list[dict[str, Any]] | None = None,
    confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
    known_target_fields: set[str] | None = None,
) -> dict[str, Any]:
    """Physically profile source fields and propose semantic roles as suggestions."""

    import re  # noqa: PLC0415

    fields_mapping = []
    ambiguities = []
    date_candidates = []
    currency_candidates = set()
    unknown_field_ids = []
    date_grains: dict[str, str] = {}

    sample_rows = sample_data or []
    sample_count = len(sample_rows)

    for item in field_records:
        field_id = item.get("field_id") or item.get("name") or "field"
        kind = item.get("kind", "dimension")
        physical_type = item.get("physical_type", "unknown")
        semantic_hints = item.get("semantic_hints", [])
        canonical_target = item.get("canonical_target")
        aggregation = item.get("aggregation", "none")
        non_additive = bool(item.get("non_additive", False))
        # Story 69.2 (AD-4): the CANONICAL vocabulary overrules the caller here.
        # `non_additive` used to come from the profiler alone, so a file column
        # literally named `ctr` or `average_position` could be declared additive
        # and be SUMMED into the mart -- a sum of rates, which is not a smaller
        # number but a false one. `dim_metric.csv` already declares which names
        # are ratios, and `is_non_additive` is the same read the dbt gate does.
        # A caller may raise the flag; it may not lower it.
        if not non_additive:
            from core.report_dictionary import is_non_additive  # noqa: PLC0415

            for candidate in (item.get("canonical_target"), field_id):
                if candidate and is_non_additive(str(candidate)):
                    non_additive = True
                    break

        type_class = _physical_type_class(physical_type, kind)

        # Profile from sample
        nullable = False
        unique = False
        cardinality_signal = "unknown"
        sample_values = []
        non_null_count = 0

        if sample_rows:
            vals = [row.get(field_id) for row in sample_rows if field_id in row]
            if len(vals) < sample_count:
                nullable = True
            non_null_vals = [v for v in vals if v is not None]
            non_null_count = len(non_null_vals)

            # Safe stringification + basic email redaction for sample values
            str_vals = []
            for v in non_null_vals[:5]:
                v_str = _canonical_json(v) if isinstance(v, (dict, list)) else str(v)
                v_redacted = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "[REDACTED_EMAIL]", v_str)
                str_vals.append(v_redacted)
            sample_values = str_vals

            # Unique values computation safe against unhashable types
            unique_vals = {
                _canonical_json(v) if isinstance(v, (dict, list)) else v for v in non_null_vals
            }
            if len(unique_vals) == sample_count and sample_count > 1:
                unique = True
                cardinality_signal = "unique"
            elif sample_count > 1 and len(unique_vals) > sample_count * 0.8:
                cardinality_signal = "high"
            elif len(unique_vals) > 5:
                cardinality_signal = "medium"
            else:
                cardinality_signal = "low"

        confidence = _derive_confidence(
            type_class=type_class,
            physical_type=physical_type,
            kind=kind,
            semantic_hints=list(semantic_hints),
            sample_count=sample_count,
            non_null_count=non_null_count,
        )

        # Determine suggestion from the recognized type class only.
        semantic_role = "dimension"
        if type_class == "date" or "primary_date" in semantic_hints:
            semantic_role = "primary_date"
            date_candidates.append(field_id)
            date_grains[field_id] = _date_granularity(physical_type)
        elif type_class == "numeric":
            if "spend" in semantic_hints or "cost" in field_id.lower():
                semantic_role = "measure_spend"
            elif (
                "revenue" in semantic_hints
                or "sales" in field_id.lower()
                or "amount" in field_id.lower()
            ):
                semantic_role = "measure_revenue"
            else:
                semantic_role = "measure"
        elif type_class == "unknown":
            unknown_field_ids.append(field_id)

        # Currency & Sensitivity
        currency = item.get("currency", "unknown")
        if currency != "unknown":
            currency_candidates.add(currency)

        sensitivity = item.get("sensitivity", "unknown")
        if sensitivity not in ("none", "pii", "financial", "credentials", "internal", "unknown"):
            sensitivity = "unknown"

        # Check evidence
        evidence = [f"kind:{kind}", f"physical_type:{physical_type}"]
        if semantic_hints:
            evidence.extend([f"hint:{h}" for h in semantic_hints])

        # Determine binding status & blocking reasons (deterministic precedence).
        binding_status = "suggested"
        blocking_reason = None

        if type_class == "unknown":
            binding_status = "blocking"
            blocking_reason = "unknown_field"
        elif confidence < confidence_threshold:
            binding_status = "blocking"
            blocking_reason = "low_confidence"
        elif (
            known_target_fields is not None
            and len(known_target_fields) > 0
            and canonical_target
            and canonical_target not in known_target_fields
        ):
            binding_status = "blocking"
            blocking_reason = f"target_field_not_found:{canonical_target}"

        mapping_aggregation, aggregation_evidence = _mapping_aggregation(aggregation)
        if aggregation_evidence:
            evidence = [*evidence, aggregation_evidence]

        fields_mapping.append(
            {
                "field_id": field_id,
                "physical_type": physical_type,
                "profile": {
                    "nullable": nullable,
                    "unique": unique,
                    "cardinality_signal": cardinality_signal,
                    "sample_values": sample_values,
                    "confidence": confidence,
                },
                "suggestion": {
                    "semantic_role": semantic_role,
                    "aggregation": mapping_aggregation,
                    "non_additive": non_additive,
                    "currency": currency,
                    "sensitivity": sensitivity,
                    "status": "suggested",
                    "evidence": evidence,
                },
                "binding": {
                    "canonical_target": canonical_target,
                    "mdm_target": None,
                    "status": binding_status,
                    "blocking_reason": blocking_reason,
                    "confirmed_by": None,
                    "confirmed_reason": None,
                },
            }
        )

    # Detect Ambiguities & Mark Affected Bindings as Blocking.
    #
    # Currency ambiguity and non-additive-bound-additively reuse the EXACT conflict
    # codes recognized by datamodel._detect_conflicts (CONFLICT_CURRENCY /
    # CONFLICT_MEASURE_NULL) so the mapping speaks the same conflict vocabulary the
    # dictionary already understands; 12.3 records the state, Epic 13 resolves it.
    if len(date_candidates) > 1:
        ambiguities.append(
            {
                "code": "multiple_date_candidates",
                "path": "$.fields",
                "field_ids": sorted(date_candidates),
                "candidates": sorted(date_candidates),
                "repair": {"choose_primary_date": sorted(date_candidates)},
            }
        )
        for f in fields_mapping:
            if f["field_id"] in date_candidates and f["binding"]["status"] != "confirmed":
                f["binding"]["status"] = "blocking"
                f["binding"]["blocking_reason"] = "multiple_date_candidates"

    # mixed_grain: date candidates observed at differing granularity (e.g. a plain
    # `date` day column alongside a `timestamp` instant column) cannot share a
    # joint grain without an explicit downcast decision.
    grained_field_ids = [
        fid for fid in date_candidates if date_grains.get(fid, "unspecified") != "unspecified"
    ]
    observed_grains = {date_grains[fid] for fid in grained_field_ids}
    if len(observed_grains) > 1:
        grain_field_ids = sorted(grained_field_ids)
        ambiguities.append(
            {
                "code": "mixed_grain",
                "path": "$.fields",
                "field_ids": grain_field_ids,
                "candidates": sorted(observed_grains),
                "repair": {"align_date_grain": "choose_single_granularity"},
            }
        )
        for f in fields_mapping:
            if f["field_id"] in grain_field_ids and f["binding"]["status"] != "confirmed":
                f["binding"]["status"] = "blocking"
                f["binding"]["blocking_reason"] = "mixed_grain"

    if len(currency_candidates) > 1:
        currency_field_ids = [
            f["field_id"]
            for f in fields_mapping
            if f["suggestion"]["currency"] != "unknown"
        ]
        ambiguities.append(
            {
                "code": CONFLICT_CURRENCY,
                "path": "$.fields",
                "field_ids": currency_field_ids,
                "candidates": sorted(list(currency_candidates)),
                "repair": {"normalize_currency_policy": "explicit_selection_required"},
            }
        )
        for f in fields_mapping:
            if f["field_id"] in currency_field_ids and f["binding"]["status"] != "confirmed":
                f["binding"]["status"] = "blocking"
                f["binding"]["blocking_reason"] = CONFLICT_CURRENCY

    for f in fields_mapping:
        if f["suggestion"]["non_additive"] and f["suggestion"]["aggregation"] == "sum":
            ambiguities.append(
                {
                    "code": CONFLICT_MEASURE_NULL,
                    "path": f"$.fields[?(@.field_id=='{f['field_id']}')]",
                    "field_ids": [f["field_id"]],
                    "candidates": ["avg", "min", "max", "custom", "none"],
                    "repair": {"change_aggregation": "avg"},
                }
            )
            if f["binding"]["status"] != "confirmed":
                f["binding"]["status"] = "blocking"
                f["binding"]["blocking_reason"] = CONFLICT_MEASURE_NULL

    # unknown_field: a field whose physical_type/kind falls outside the recognized
    # universe is surfaced (not silently classed as a dimension) and blocks.
    if unknown_field_ids:
        ambiguities.append(
            {
                "code": "unknown_field",
                "path": "$.fields",
                "field_ids": sorted(unknown_field_ids),
                "candidates": sorted(unknown_field_ids),
                "repair": {"declare_physical_type_or_kind": sorted(unknown_field_ids)},
            }
        )

    grain = [
        f["field_id"]
        for f in fields_mapping
        if f["suggestion"]["semantic_role"] in ("primary_date", "dimension")
        and f["field_id"] not in unknown_field_ids
    ]
    source_schema_hash = compute_source_schema_hash(field_records)

    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": source_schema_hash,
        "plan_version_id": "draft_plan",
        "capability_fingerprint": None,
        "grain": sorted(grain),
        "fields": fields_mapping,
        "ambiguities": ambiguities,
    }
    return payload


def confirm_binding(
    mapping: dict[str, Any],
    field_id: str,
    *,
    actor: str,
    reason: str,
    canonical_target: str | None = None,
    mdm_target: str | None = None,
) -> dict[str, Any]:
    """Confirm a field binding with explicit actor and reason."""
    if not str(actor).strip() or not str(reason).strip():
        raise ValueError("actor and reason are required to confirm a binding")

    updated = deepcopy(mapping)
    found = False
    for f in updated.get("fields", []):
        if f.get("field_id") == field_id:
            found = True
            binding = f.setdefault("binding", {})
            binding["status"] = "confirmed"
            binding["confirmed_by"] = actor
            binding["confirmed_reason"] = reason
            binding["blocking_reason"] = None
            if canonical_target is not None:
                binding["canonical_target"] = canonical_target
            if mdm_target is not None:
                binding["mdm_target"] = mdm_target
            break

    if not found:
        raise ValueError(f"field {field_id!r} not found in mapping")
    return updated


def project_ossie(
    mapping_payload: dict[str, Any], dataset_name: str = "default_dataset"
) -> dict[str, Any]:
    """Emit deterministic Apache Ossie 0.1.1 semantic model projection."""
    fields_proj = []
    metrics_proj = []

    for f in mapping_payload.get("fields", []):
        name = f["field_id"]
        sugg = f.get("suggestion") or {}
        bind = f.get("binding") or {}
        prof = f.get("profile") or {}

        toorow_ext = {
            "toorow_extension_version": "1",
            "physical_type": f.get("physical_type", "unknown"),
            "aggregation": sugg.get("aggregation", "none"),
            "non_additive": sugg.get("non_additive", False),
            "currency": sugg.get("currency", "unknown"),
            "sensitivity": sugg.get("sensitivity", "unknown"),
            "confidence": prof.get("confidence", 0.0),
            "canonical_target": bind.get("canonical_target"),
            "mdm_target": bind.get("mdm_target"),
            "binding_status": bind.get("status", "suggested"),
        }

        field_obj = {
            "name": name,
            "description": f"Field {name} ({sugg.get('semantic_role', 'dimension')})",
            "ai_context": (
                f"Role: {sugg.get('semantic_role')}; "
                f"Aggregation: {sugg.get('aggregation')}"
            ),
            "custom_extensions": [
                {
                    "vendor_name": "toorow",
                    "data": _canonical_json(toorow_ext),
                }
            ],
        }
        fields_proj.append(field_obj)

        # Metrics for confirmed/resolved additive measures
        agg = (sugg.get("aggregation") or "none").lower()
        if (
            sugg.get("semantic_role", "").startswith("measure")
            and not sugg.get("non_additive", False)
            and agg in ("sum", "avg", "min", "max", "count")
            and bind.get("status") in ("confirmed", "resolved")
        ):
            metrics_proj.append(
                {
                    "name": f"{name}_{agg}",
                    "description": f"{agg.title()} of {name}",
                    "ai_context": f"Additive {agg} metric for {name}",
                    "dataset": dataset_name,
                    "expression": f"{agg.upper()}({name})",
                    "custom_extensions": [
                        {
                            "vendor_name": "toorow",
                            "data": _canonical_json({"source_field": name}),
                        }
                    ],
                }
            )

    projection = {
        "ossie_spec_version": "0.1.1",
        "semantic_model": {
            "description": "toorow Datastream mapping projection",
            "ai_context": "Generated by toorow Datastream field mapping service",
            "datasets": [
                {
                    "name": dataset_name,
                    "description": f"Dataset projection for {dataset_name}",
                    "fields": fields_proj,
                }
            ],
            "relationships": [],
            "metrics": metrics_proj,
        },
    }

    # Validate against Ossie profile schema
    validator = _ossie_schema_validator()
    errors = list(validator.iter_errors(projection))
    if errors:
        raise ValueError(f"Ossie projection validation failed: {errors[0].message}")

    return projection


def _version_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "datastream_id",
        "project_id",
        "version_number",
        "mapping_contract_version",
        "source_schema_hash",
        "plan_version_id",
        "capability_fingerprint",
        "content_hash",
        "ossie_spec_version",
        "toorow_extension_version",
        "executable",
        "blocking_count",
        "mapping_payload",
        "ossie_projection",
        "created_by",
        "created_at",
    )
    result = dict(zip(keys, row))
    if result.get("created_at") is not None:
        result["created_at"] = result["created_at"].isoformat()
    return result


def _pin_primary_date(conn: Any, project_id: str, normalized_payload: dict[str, Any]) -> bool:
    """Pin every `primary_date` field to the visible canonical `date`, unless already pinned.

    THE PRIMARY DATE IS THE CANONICAL DATE, BY CONSTRUCTION (2026-09-05). A column
    the profile recognised as `primary_date` means one thing in every flow of
    every project, and the product pins it itself -- a person may only re-point
    it. Measured 2026-09-04 on the reference project: ten flows carry `date`,
    none of the published mappings pinned it, and the crossing waited on ten
    identical human gestures. Jean: « les champs date sont forcément et
    obligatoirement épinglables ». The Project's own `date` wins over the
    platform's when both exist. Fail-soft: an unreadable registry pins nothing,
    and the resolution that follows says so per binding. Returns whether a pin
    was added.
    """
    candidates = [
        f
        for f in normalized_payload.get("fields", [])
        if isinstance(f, dict)
        and (f.get("suggestion") or {}).get("semantic_role") == "primary_date"
        and (f.get("binding") or {}).get("status") != "excluded"
        and not (f.get("binding") or {}).get("mdm_target")
    ]
    if not candidates:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT sp_primary_date_pin")
            cur.execute(
                """
                SELECT id FROM app.mdm_canonical_fields
                 WHERE canonical_name = 'date' AND concept_kind = 'dimension'
                   AND status = 'active'
                   AND (project_id = %s OR project_id IS NULL)
                 ORDER BY (project_id IS NULL), id
                 LIMIT 1
                """,
                (project_id,),
            )
            row = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT sp_primary_date_pin")
    except Exception:  # noqa: BLE001 -- the registry decides; unreadable means no pin
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT sp_primary_date_pin")
        except Exception:  # noqa: BLE001
            pass
        return False
    if row is None:
        return False
    for field in candidates:
        field.setdefault("binding", {})["mdm_target"] = str(row[0])
    return True


def save_field_mapping(
    *,
    datastream_id: str,
    project_id: str,
    mapping_payload: dict[str, Any],
    identity: str,
    idempotency_key: str,
    conn: Any,
    dataset_name: str = "default_dataset",
    trace_id: str | None = None,
    pinned_plan_version_id: str | None = None,
    advance_pointer: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """Append one immutable mapping version, optionally without activating it."""

    if not idempotency_key.strip():
        raise ValueError("Idempotency-Key is required")
    idempotency_hash = _sha256(idempotency_key)

    normalized_payload, content_hash = normalize_mapping(mapping_payload)
    # The pin is part of the version, so the hash is the hash of what is stored:
    # normalised again after it, never before.
    if _pin_primary_date(conn, project_id, normalized_payload):
        normalized_payload, content_hash = normalize_mapping(normalized_payload)

    # Story 60.6: joins and splits are refused HERE, before a version exists.
    #
    # The structural half is the JSON schema's (two sources minimum, two targets
    # minimum, one pattern per split). What the schema cannot see is the payload
    # around the declaration: a source column this mapping does not carry, a
    # source somebody excluded, a pattern that does not compile, or two rules
    # claiming one concept. `csv_excel_import` already refuses the last one --
    # `dispatch_mapping_collision` -- but it refuses it while landing rows, which
    # is after the fact. Refusing at the append is what makes it "before the
    # import" (story 60.6, Refuse).
    from core.column_treatments import normalize_treatments  # noqa: PLC0415

    normalize_treatments(normalized_payload)

    # Registry resolution (read-only). Each binding that names an mdm_target must
    # resolve to a LIVE, IN-SCOPE row of app.mdm_canonical_fields (platform-scoped
    # project_id IS NULL, or the same project). A missing/archived/out-of-scope
    # target makes the binding blocking with a stable repair. This path writes ZERO
    # registry rows (Epic-13 governance boundary); registry is minted elsewhere.
    #
    # Fail-closed: if the registry lookup itself fails or the registry cannot be
    # read, every mdm-bound binding blocks rather than passing.
    mdm_targets = {
        b.get("mdm_target")
        for f in normalized_payload.get("fields", [])
        if (b := f.get("binding") or {}).get("mdm_target")
    }
    resolved_registry: dict[str, str] = {}
    # Story 68.2: the same read also yields each canonical field's object_kind
    # (migration 241), which the designation validation below needs to refuse a
    # column that answers "which object" twice (AC5). Read-only, like the rest.
    mdm_object_kinds: dict[str, Any] = {}
    registry_lookup_failed = False
    if mdm_targets:
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_mdm_registry")
                cur.execute(
                    """
                    SELECT id, status, project_id, object_kind
                    FROM app.mdm_canonical_fields
                    WHERE id = ANY(%s) AND (project_id IS NULL OR project_id = %s)
                    """,
                    (list(mdm_targets), project_id),
                )
                for row_id, status, _proj, object_kind in cur.fetchall():
                    if status == "active":
                        resolved_registry[row_id] = status
                    mdm_object_kinds[row_id] = object_kind
                cur.execute("RELEASE SAVEPOINT sp_mdm_registry")
        except Exception:
            registry_lookup_failed = True
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_mdm_registry")
            except Exception:
                pass

    # Entity designations (Story 68.2), validated through the SAME fail-closed
    # posture as the mdm_target resolution above: the lookup reads the registry
    # Story 68.1 writes and writes NOTHING itself (the Epic-13 boundary stands);
    # a designation that cannot be honored marks its binding `blocking` with a
    # stable reason; an unreadable registry blocks every designating binding
    # rather than passing. The version is born non-executable, never repaired
    # in place -- mappings are immutable, so the repair is a new declaration.
    designation_issues: dict[str, EntityDesignationIssue] = {}
    designated_kinds = {
        kind for _field_id, kind in entity_designations(normalized_payload)
    }
    if designated_kinds:
        declared_types: dict[str, Any] | None = None
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_entity_types")
                declared_types = fetch_entity_type_lookup(
                    conn, project_id=project_id, object_kinds=sorted(designated_kinds)
                )
                cur.execute("RELEASE SAVEPOINT sp_entity_types")
        except Exception:
            declared_types = None
            try:
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_entity_types")
            except Exception:
                pass
        if declared_types is None:
            for field_id, kind in entity_designations(normalized_payload):
                designation_issues.setdefault(
                    field_id,
                    EntityDesignationIssue(
                        field_id=field_id,
                        object_kind=kind,
                        reason=REASON_LOOKUP_FAILED,
                        message=(
                            "the entity type registry could not be read, so this "
                            "designation cannot be verified -- retry rather than "
                            "publishing an unverified key"
                        ),
                    ),
                )
        else:
            for issue in validate_entity_designations(
                normalized_payload,
                declared_types=declared_types,
                mdm_object_kinds=mdm_object_kinds,
            ):
                designation_issues.setdefault(issue.field_id, issue)

    # Event roles (Story 68.4), validated at append like the designation above
    # -- and PURELY: the event vocabulary is a repo catalogue, not a
    # project-scoped table, so nothing here reads the database and nothing can
    # drift between this check and the import. A broken declaration marks its
    # column blocking; a declaration missing a required role has no column to
    # blame, so it blocks the columns that DID declare a role -- the version is
    # born non-executable either way, which is the only state that stops it.
    event_issues: dict[str, str] = {}
    event_declaration_issues = validate_event_declaration(normalized_payload)
    if event_declaration_issues:
        declared_event_fields = [
            field_id for field_id, _role, _target in event_roles(normalized_payload)
        ]
        for issue in event_declaration_issues:
            if issue.field_id:
                event_issues.setdefault(issue.field_id, issue.reason)
            else:
                for field_id in declared_event_fields:
                    event_issues.setdefault(field_id, issue.reason)

    # Apply registry resolution to each binding.
    blocking_count = 0
    for f in normalized_payload.get("fields", []):
        binding = f.get("binding", {})
        mdm_target = binding.get("mdm_target")
        if mdm_target:
            if registry_lookup_failed or mdm_target not in resolved_registry:
                # Missing, archived, out-of-scope, or unreadable registry: fail-closed.
                if binding.get("status") not in ("excluded",):
                    binding["status"] = "blocking"
                    binding["blocking_reason"] = "register_or_pick_canonical_field"

        designation_issue = designation_issues.get(f.get("field_id"))
        if designation_issue is not None and binding.get("status") not in ("excluded",):
            binding["status"] = "blocking"
            binding["blocking_reason"] = (
                f"{designation_issue.reason}:{designation_issue.object_kind}"
            )

        event_issue = event_issues.get(f.get("field_id"))
        if event_issue is not None and binding.get("status") not in ("excluded",):
            binding["status"] = "blocking"
            binding["blocking_reason"] = event_issue

        if binding.get("status") == "blocking":
            blocking_count += 1

    ambiguity_count = len(normalized_payload.get("ambiguities", []))
    executable = (blocking_count == 0) and (ambiguity_count == 0)

    # Generate Ossie projection
    ossie_proj = project_ossie(normalized_payload, dataset_name=dataset_name)
    mapping_id = _mint_mapping_id()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, current_plan_version_id, current_mapping_version_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (datastream_id, project_id),
            )
            datastream_row = cur.fetchone()
            if datastream_row is None:
                raise DatastreamMappingNotFound("Datastream not found")

            plan_version_id = pinned_plan_version_id or datastream_row[1]
            previous_mapping_version_id = datastream_row[2]
            if not plan_version_id:
                raise DatastreamMappingUnavailable("Datastream has no current plan version")

            # Check plan version details
            cur.execute(
                """
                SELECT capability_fingerprint
                FROM app.datastream_plan_versions
                WHERE id = %s AND datastream_id = %s AND project_id = %s
                """,
                (plan_version_id, datastream_id, project_id),
            )
            plan_row = cur.fetchone()
            capability_fingerprint = plan_row[0] if plan_row else None
            normalized_payload["plan_version_id"] = plan_version_id
            normalized_payload["capability_fingerprint"] = capability_fingerprint

            # Re-hash content after binding updates
            normalized_payload, content_hash = normalize_mapping(normalized_payload)

            # Idempotency check
            cur.execute(
                """
                SELECT id, datastream_id, project_id, version_number, mapping_contract_version,
                       source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                       ossie_spec_version, toorow_extension_version, executable, blocking_count,
                       mapping_payload, ossie_projection, created_by, created_at
                FROM app.datastream_mapping_versions
                WHERE project_id = %s AND idempotency_key_hash = %s
                """,
                (project_id, idempotency_hash),
            )
            existing = cur.fetchone()
            if existing is not None:
                result = _version_dict(existing)
                if (
                    result["datastream_id"] != datastream_id
                    or result["content_hash"] != content_hash
                ):
                    raise DatastreamMappingConflict("idempotency_conflict")
                result["idempotent_replay"] = True
                return result

            # Allocate version number
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM app.datastream_mapping_versions
                WHERE datastream_id = %s AND project_id = %s
                """,
                (datastream_id, project_id),
            )
            version_number = int(cur.fetchone()[0])

            # Insert version
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_versions
                    (id, datastream_id, project_id, version_number, mapping_contract_version,
                     source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                     ossie_spec_version, toorow_extension_version, executable, blocking_count,
                     mapping_payload, ossie_projection, idempotency_key_hash, created_by)
                VALUES
                    (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, %s
                    )
                RETURNING id, datastream_id, project_id, version_number, mapping_contract_version,
                          source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                          ossie_spec_version, toorow_extension_version, executable, blocking_count,
                          mapping_payload, ossie_projection, created_by, created_at
                """,
                (
                    mapping_id,
                    datastream_id,
                    project_id,
                    version_number,
                    normalized_payload["mapping_contract_version"],
                    normalized_payload["source_schema_hash"],
                    plan_version_id,
                    capability_fingerprint,
                    content_hash,
                    "0.1.1",
                    "1",
                    executable,
                    blocking_count,
                    _canonical_json(normalized_payload),
                    _canonical_json(ossie_proj),
                    idempotency_hash,
                    identity,
                ),
            )
            inserted = cur.fetchone()

            # Update pointer in app.datastreams
            cur.execute(
                """
                UPDATE app.datastreams
                SET current_mapping_version_id = %s
                WHERE id = %s AND project_id = %s AND %s
                """,
                (mapping_id, datastream_id, project_id, advance_pointer),
            )

        from core.audit import (  # noqa: PLC0415
            insert_audit_row,
        )

        insert_audit_row(
            conn,
            identity=identity,
            action=ACTION_DATASTREAM_MAPPING_VERSIONED,
            provider_account="",
            connection_ref="",
            metadata={
                "project_id": project_id,
                "datastream_id": datastream_id,
                "plan_version_id": plan_version_id,
                "previous_mapping_version_id": previous_mapping_version_id,
                "current_mapping_version_id": mapping_id,
                "idempotency_key_hash": idempotency_hash,
                "source_schema_hash": normalized_payload["source_schema_hash"],
                "blocking_count": blocking_count,
                "capability_fingerprint": capability_fingerprint,
                "trace_id": trace_id,
            },
        )
        if commit:
            conn.commit()
    except (DatastreamMappingConflict, DatastreamMappingNotFound, DatastreamMappingUnavailable):
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        # Map ONLY the idempotency unique constraint to 409. A version-ordinal
        # unique violation (two concurrent revisions racing on version_number) is a
        # transient serialization failure, NOT an idempotency conflict — reporting
        # it as 409 would mislead the caller. Inspect the psycopg3 diagnostics'
        # constraint name to distinguish them precisely.
        constraint_name = ""
        diag = getattr(exc, "diag", None)
        if diag is not None:
            constraint_name = (getattr(diag, "constraint_name", "") or "").lower()
        if constraint_name == "uq_datastream_mapping_idempotency":
            raise DatastreamMappingConflict("idempotency_conflict") from exc
        if constraint_name:
            # A known non-idempotency constraint (version ordinal, FK, etc.).
            raise DatastreamMappingUnavailable("mapping persistence failed") from exc
        # No structured diagnostics (e.g. a mock without .diag): fall back to a
        # conservative substring probe scoped to the idempotency constraint only.
        err_msg = str(exc).lower()
        if "uq_datastream_mapping_idempotency" in err_msg:
            raise DatastreamMappingConflict("idempotency_conflict") from exc
        raise DatastreamMappingUnavailable("mapping persistence failed") from exc

    result = _version_dict(inserted)
    result["idempotent_replay"] = False
    return result


def list_mapping_versions(datastream_id: str, project_id: str, conn: Any) -> list[dict[str, Any]]:
    """Return immutable mapping versions newest-first within one project scope."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s",
            (datastream_id, project_id),
        )
        if cur.fetchone() is None:
            raise DatastreamMappingNotFound("Datastream not found")

        cur.execute(
            """
            SELECT id, datastream_id, project_id, version_number, mapping_contract_version,
                   source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                   ossie_spec_version, toorow_extension_version, executable, blocking_count,
                   mapping_payload, ossie_projection, created_by, created_at
            FROM app.datastream_mapping_versions
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (datastream_id, project_id),
        )
        return [_version_dict(row) for row in cur.fetchall()]


def get_mapping_version(
    datastream_id: str, project_id: str, version_spec: str | int, conn: Any
) -> dict[str, Any]:
    """Fetch one specific mapping version by ID or integer version_number."""
    with conn.cursor() as cur:
        if isinstance(version_spec, int) or (
            isinstance(version_spec, str) and version_spec.isdigit()
        ):
            v_num = int(version_spec)
            cur.execute(
                """
                SELECT id, datastream_id, project_id, version_number, mapping_contract_version,
                       source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                       ossie_spec_version, toorow_extension_version, executable, blocking_count,
                       mapping_payload, ossie_projection, created_by, created_at
                FROM app.datastream_mapping_versions
                WHERE datastream_id = %s AND project_id = %s AND version_number = %s
                """,
                (datastream_id, project_id, v_num),
            )
        else:
            cur.execute(
                """
                SELECT id, datastream_id, project_id, version_number, mapping_contract_version,
                       source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                       ossie_spec_version, toorow_extension_version, executable, blocking_count,
                       mapping_payload, ossie_projection, created_by, created_at
                FROM app.datastream_mapping_versions
                WHERE datastream_id = %s AND project_id = %s AND id = %s
                """,
                (datastream_id, project_id, str(version_spec)),
            )

        row = cur.fetchone()
        if row is None:
            raise DatastreamMappingNotFound("mapping version not found")
        return _version_dict(row)
