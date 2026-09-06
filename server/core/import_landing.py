"""toorow -- from a parsed result to rows in the warehouse.

The three acts between the parse and the ledger:

  * ``_parse_evidence`` -- the deterministic evidence persisted on the execution;
  * ``_apply_governed_mapping`` -- apply the PINNED mapping/projection bundle so
    transport bytes become a governed candidate (Story 38.13); re-deriving a
    mapping at arrival is the silent re-map AD-8 forbids, and does not happen;
  * ``_land_managed_rows`` -- the physical write, through ``raw_landing`` (the
    same seam the connectors use), so it reaches BigQuery as well as DuckDB and
    honours an active ``candidate_execution`` isolation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from typing import Any

from core.file_source_resolution import (
    _CONFIRMED_BINDING_STATES,
)
from core.file_source_template import PLAN_LINE_FIELDS
from core.tabular_parsing import (
    _coerce_typed,
    _finalize_parse_result,
    _truncate_value,
)
from core.tabular_types import (
    ColumnSpec,
    CsvExcelImportError,
    ParseResult,
    RejectedRow,
)

logger = logging.getLogger(__name__)


def _parse_evidence(result: ParseResult) -> dict[str, Any]:
    """Safe deterministic evidence persisted on the existing import execution."""
    return {
        "envelope_version": result.envelope_version,
        "producer_format": result.source_format,
        "encoding": result.encoding,
        "delimiter": result.delimiter,
        "sheet_name": result.sheet_name,
        "cell_range": result.metadata.get("cell_range"),
        "parser_policy": result.metadata.get("parser_policy", {}),
        "observed_row_count": result.detected_row_count,
        "accepted_row_count": result.accepted_count,
        "rejected_row_count": result.rejected_count,
        "content_fingerprint": result.content_hash,
        "schema_fingerprint": result.schema_fingerprint,
        "columns": [
            {
                "source_name": col.source_name,
                "source_index": col.source_index,
                "name": col.name,
                "type": col.detected_type,
                "source_type": col.source_type,
                "date_format": col.date_format,
                "locale": col.locale,
                "null_count": col.null_count,
            }
            for col in result.columns
        ],
        "warning_codes": list(result.warnings),
        "issues": [issue.as_dict() for issue in result.issues],
        "format_evidence": {
            key: result.metadata[key]
            for key in (
                "file_encoding",
                "file_format",
                "variable_labels",
                "value_labels",
                "missing_ranges",
                "missing_evidence",
                "user_missing_applied",
                "row_count_declared",
                "column_count_declared",
                "applied_decisions",
                "budgets",
            )
            if key in result.metadata
        },
    }


_MAPPING_TYPE_ALIASES = {
    "string": "text",
    "text": "text",
    "varchar": "text",
    "integer": "integer",
    "int": "integer",
    "int64": "integer",
    "decimal": "decimal",
    "float": "decimal",
    "float64": "decimal",
    "number": "decimal",
    "date": "date",
    "boolean": "boolean",
    "bool": "boolean",
}
_WAREHOUSE_TYPES = {
    "text": "STRING",
    "mixed": "STRING",
    "integer": "INTEGER",
    "decimal": "FLOAT",
    "date": "DATE",
    "boolean": "BOOLEAN",
}


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _apply_governed_mapping(
    result: ParseResult,
    *,
    mapping_payload: dict[str, Any],
    projection_plan: dict[str, Any],
    plan_version_id: str,
    mapping_version_id: str,
    row_validator: Any = None,
) -> ParseResult:
    """Apply one pinned mapping to produce the typed Universal candidate.

    Mapping proposals are not executable. Every included source field needs a
    settled binding and every target is unique. The projection is checked against
    the same plan/mapping pins before any candidate row can reach a warehouse.

    ``row_validator`` (Story 68.5) is an optional per-row veto the ROUTE
    supplies -- ``(row, row_number) -> RejectedRow | None``. It runs inside
    this function's own enumeration on purpose: a reference file rejects its
    key-less rows here, and both rejection classes (route veto and type
    coercion) carry line numbers from the SAME walk over the file. Two walks
    would disagree on the numbers the moment one of them dropped a row, and
    the ledger's per-row evidence would name the wrong line.
    """
    if not isinstance(mapping_payload, dict) or not mapping_payload.get("fields"):
        raise CsvExcelImportError(
            "dispatch_mapping_missing",
            "the pinned mapping payload is unavailable",
            repair={"recompile_mapping": True},
        )
    if not projection_plan.get("executable", False):
        raise CsvExcelImportError(
            "dispatch_projection_not_executable",
            "the pinned projection is not executable",
            repair={"resolve_projection_issues": True},
        )
    if projection_plan.get("plan_version_id") not in (None, "", plan_version_id):
        raise CsvExcelImportError(
            "dispatch_bundle_mismatch",
            "projection plan version differs from the pinned plan",
            repair={"reload_pinned_bundle": True},
        )
    if projection_plan.get("mapping_version_id") not in (
        None,
        "",
        mapping_version_id,
    ):
        raise CsvExcelImportError(
            "dispatch_bundle_mismatch",
            "projection mapping version differs from the pinned mapping",
            repair={"reload_pinned_bundle": True},
        )

    source_specs = {column.name: column for column in result.columns}
    bindings: list[tuple[str, str, ColumnSpec]] = []
    targets: set[str] = set()
    for item in mapping_payload.get("fields") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("field_id") or "").strip()
        binding = item.get("binding") or {}
        status = str(binding.get("status") or "").lower()
        if status == "excluded":
            continue
        target = str(binding.get("canonical_target") or "").strip()
        if status not in _CONFIRMED_BINDING_STATES or not source or not target:
            raise CsvExcelImportError(
                "dispatch_mapping_unconfirmed",
                "the pinned mapping contains an unconfirmed included field",
                repair={"source_field": source or None, "confirm_or_exclude": True},
            )
        if target in targets:
            raise CsvExcelImportError(
                "dispatch_mapping_collision",
                "multiple source fields map to one candidate column",
                repair={"canonical_target": target},
            )
        if source not in source_specs:
            raise CsvExcelImportError(
                "dispatch_source_field_missing",
                "a pinned source field is absent from the parsed envelope",
                repair={"source_field": source, "review_drift": True},
            )
        targets.add(target)
        physical = str(item.get("physical_type") or "").strip().lower()
        detected = _MAPPING_TYPE_ALIASES.get(physical)
        if detected not in _WAREHOUSE_TYPES:
            raise CsvExcelImportError(
                "dispatch_type_unsupported",
                "the pinned mapping physical type is unsupported",
                repair={"source_field": source, "physical_type": physical},
            )
        original = source_specs[source]
        bindings.append(
            (
                source,
                target,
                ColumnSpec(
                    name=target,
                    index=len(bindings),
                    detected_type=detected,
                    date_format=original.date_format,
                    null_count=original.null_count,
                    sample_values=list(original.sample_values),
                    source_name=source,
                    source_index=original.index,
                    source_type=original.detected_type,
                    locale=original.locale,
                ),
            )
        )

    if not bindings:
        raise CsvExcelImportError(
            "dispatch_projection_empty",
            "the pinned mapping excludes every candidate field",
            repair={"include_at_least_one_field": True},
        )

    relation = projection_plan.get("full_grain_relation") or {}
    grain_sources = [
        str(item.get("field_id") or "").strip()
        for item in relation.get("grain_columns") or []
        if isinstance(item, dict)
    ]
    source_order = [
        *grain_sources,
        *[
            str(value).strip()
            for value in relation.get("source_fields") or []
            if str(value).strip() not in grain_sources
        ],
    ]
    additive_sources = {
        str(item.get("field_id") or "").strip()
        for item in projection_plan.get("additive_measures") or []
        if isinstance(item, dict)
    }
    governed = projection_plan.get("governed_dimension_projection")
    governed_source = (
        str(governed.get("field_id") or "").strip()
        if isinstance(governed, dict)
        else ""
    )
    required_projection_fields = {
        *source_order,
        *additive_sources,
        *([governed_source] if governed_source else []),
    }
    binding_by_source = {source: (source, target, spec) for source, target, spec in bindings}
    missing_projection_fields = sorted(
        field for field in required_projection_fields if field not in binding_by_source
    )
    extra_mapping_fields = sorted(set(binding_by_source) - set(source_order))
    if missing_projection_fields or extra_mapping_fields:
        raise CsvExcelImportError(
            "dispatch_projection_field_mismatch",
            "the pinned full-grain projection and mapping fields diverge",
            repair={
                "missing_fields": missing_projection_fields,
                "extra_fields": extra_mapping_fields,
                "recompile_projection": True,
            },
        )
    bindings = [binding_by_source[source] for source in source_order]
    if "grain_key" in targets:
        raise CsvExcelImportError(
            "dispatch_projection_reserved_field",
            "the mapping targets the reserved full-grain identity column",
            repair={"canonical_target": "grain_key"},
        )
    grain_targets = [binding_by_source[source][1] for source in grain_sources]
    if not grain_targets:
        raise CsvExcelImportError(
            "dispatch_projection_grain_missing",
            "the executable full-grain relation has no grain columns",
            repair={"recompile_projection": True},
        )

    mapped_rows: list[dict[str, Any]] = []
    seen_grain_keys: set[str] = set()
    rejected = list(result.rejected)
    for row_number, row in enumerate(result.rows, start=2):
        if row_validator is not None:
            veto = row_validator(row, row_number)
            if veto is not None:
                rejected.append(veto)
                continue
        mapped: dict[str, Any] = {}
        failed = False
        for source, target, spec in bindings:
            try:
                mapped[target] = _coerce_typed(row.get(source), spec)
            except (TypeError, ValueError):
                rejected.append(
                    RejectedRow(
                        row_number=row_number,
                        field_name=source,
                        rule="dispatch_type_mismatch",
                        reason="value does not satisfy the pinned mapping type",
                        rejected_value=_truncate_value(row.get(source)),
                    )
                )
                failed = True
                break
        if not failed:
            grain_key = _canonical_fingerprint(
                [mapped.get(target) for target in grain_targets]
            )
            if grain_key in seen_grain_keys:
                raise CsvExcelImportError(
                    "dispatch_projection_grain_collision",
                    "multiple rows share the declared full-grain identity",
                    repair={"grain_fields": grain_sources},
                )
            seen_grain_keys.add(grain_key)
            mapped["grain_key"] = grain_key
            mapped_rows.append(mapped)

    projected_columns = [spec for _, _, spec in bindings]
    projected_columns.append(
        ColumnSpec(
            name="grain_key",
            index=len(projected_columns),
            detected_type="text",
        )
    )
    mapped = ParseResult(
        rows=mapped_rows,
        rejected=rejected,
        columns=projected_columns,
        encoding=result.encoding,
        delimiter=result.delimiter,
        sheet_name=result.sheet_name,
        detected_row_count=result.detected_row_count,
        content_hash=result.content_hash,
        metadata={
            **result.metadata,
            "mapping_version_id": mapping_version_id,
            "plan_version_id": plan_version_id,
            "projection_contract_version": projection_plan.get(
                "projection_contract_version"
            ),
            "source_schema_fingerprint": result.schema_fingerprint,
            "full_grain_relation_applied": True,
            "grain_sources": grain_sources,
            "additive_measures_applied": sorted(additive_sources),
            "governed_dimension_projection_applied": governed,
        },
        envelope_version="universal-datastream-candidate-v1",
        source_format=result.source_format,
        warnings=list(result.warnings),
        issues=list(result.issues),
    )
    return _finalize_parse_result(mapped)


def _candidate_content_fingerprint(result: ParseResult) -> str:
    return _canonical_fingerprint(
        {
            "envelope_version": result.envelope_version,
            "columns": [
                {
                    "name": column.name,
                    "type": column.detected_type,
                    "source_name": column.source_name,
                }
                for column in result.columns
            ],
            "rows": result.rows,
        }
    )


# ---------------------------------------------------------------------------
# The plan-store landing target (chantier 67-25b).
#
# `file-source-ingestion.md` ratified ONE ingestion engine, and a media plan as a
# template PROFILE of it. The consequence this seam carries: a template may
# declare `landing_target: "plan_store"`, and the SAME `run_import()` chain then
# writes a new plan version instead of a warehouse relation.
#
# It is not a second landing implementation -- it is a second TARGET of the one
# chain. The versioned write it delegates to is the plan store's own
# `create_version_with_lines`, unchanged: the convergence changes the ingestion
# PATH, never the plan's data model, so `mediaplan_store`, `mediaplan_mapping`,
# `datastream_workbench_placements` and `plan_actual_alignment` keep reading
# exactly what they read before.
# ---------------------------------------------------------------------------

#: Cell values that read as "yes" for a boolean plan-line flag. Kept identical to
#: what the pre-convergence parser accepted, so a workbook classified one way
#: before the fusion is classified the same way after it.
_PLAN_TRUE_VALUES = frozenset(
    {"1", "true", "vrai", "oui", "yes", "x", "plan-only", "plan only"}
)

def _coerce_plan_flag(value: Any) -> bool:
    """Coerce a cell value to a plan-line boolean.

    `bool("false")` is True, so a reshaped cell cannot be handed to the store
    raw: the string 'false' would land as plan-only. The truthy vocabulary is the
    parser's own, so the coercion does not drift between the two paths.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in _PLAN_TRUE_VALUES


def land_plan_store_rows(
    *,
    rows: list[dict[str, Any]],
    plan_id: str,
    actor: str,
    conn: Any,
    source_note: str | None = None,
) -> dict[str, Any]:
    """Land plan-line rows as a new CANDIDATE version of ``plan_id``.

    Two-step by design, and the design is the plan store's, not a second one: this
    creates a candidate and NEVER publishes. The currently published version stays
    intact whatever happens, and publication remains the explicit governed act it
    already was. That is the single publication lifecycle a plan import has.

    Returns the same shape ``_land_managed_rows`` returns, so the ledger records
    a plan-store landing exactly as it records a warehouse one.
    """
    from core.mediaplan_store import create_version_with_lines  # noqa: PLC0415

    payload: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        line: dict[str, Any] = {"sort_order": index}
        for field_name in PLAN_LINE_FIELDS:
            if field_name not in row:
                continue
            value = row[field_name]
            if field_name == "is_plan_only":
                line[field_name] = _coerce_plan_flag(value)
            elif isinstance(value, (date, datetime)):
                line[field_name] = value.isoformat()
            else:
                line[field_name] = value
        payload.append(line)

    version = create_version_with_lines(
        conn,
        plan_id=plan_id,
        lines=payload,
        source_note=source_note or "file-source import",
        created_by=actor,
    )
    return {
        "rows": len(payload),
        # The ledger's `landing_relation` is a free string it never parses. Naming
        # the exact version makes the plan landing as traceable as a relation.
        "table": f"app.media_plan_versions:{version['id']}",
        "backend": "plan_store",
        "execution_id": None,
        "plan_version": version,
    }


def _land_managed_rows(
    *,
    landing_relation: str,
    rows: list[dict[str, Any]],
    project_id: str,
    columns: list[ColumnSpec] | None = None,
) -> dict[str, Any]:
    """Write accepted managed-feed rows to the project-scoped raw landing.

    `landing_relation` arrives qualified (`schema.table`) because the ledger
    allocates it that way; `land_raw_rows` resolves the dataset/schema itself
    from the Project, so it takes the bare table name. Composing a warehouse
    location here would be a connector deciding where the raw zone lives.
    """
    from core.raw_landing import land_raw_rows  # noqa: PLC0415

    if not rows:
        return {"rows": 0, "table": landing_relation}

    table = landing_relation.rsplit(".", 1)[-1]
    # Ordered union of the keys actually present. A row missing a column lands
    # NULL rather than shifting the row, which is what a positional list would
    # do the first time one upload omits an optional field.
    column_names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                column_names.append(str(key))

    spec_by_name = {column.name: column for column in (columns or [])}

    def warehouse_value(name: str, value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if name not in spec_by_name and value is not None:
            return str(value)
        return value

    typed_rows = [
        {key: warehouse_value(key, value) for key, value in row.items()}
        for row in rows
    ]
    warehouse_columns = [
        (
            name,
            _WAREHOUSE_TYPES.get(
                spec_by_name[name].detected_type, "STRING"
            )
            if name in spec_by_name
            else "STRING",
        )
        for name in column_names
    ]
    return land_raw_rows(
        table,
        typed_rows,
        columns=warehouse_columns,
        project_id=project_id,
    )
