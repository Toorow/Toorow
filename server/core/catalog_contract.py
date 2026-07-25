"""Generic validation and diff engine for connector API field catalogs.

This module deliberately contains no connector identifiers, provider names,
or field vocabulary. All provider-specific knowledge lives in api_catalog.json
files; core only enforces the versioned contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "api-catalog.schema.json"
_SCHEMA_ID = "https://toorow.dev/schemas/api-catalog.schema.json"
_CATALOG_FILE = "api_catalog.json"


@dataclass(frozen=True, order=True)
class CatalogIssue:
    """One deterministic, actionable catalog-contract problem."""

    path: str
    code: str
    message: str
    suggested_repair: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "suggested_repair": self.suggested_repair,
        }


class CatalogValidationError(ValueError):
    """Raised when a catalog cannot be validated safely."""

    def __init__(self, issues: list[CatalogIssue]):
        self.issues = sorted(issues)
        super().__init__("; ".join(f"{item.path}: {item.message}" for item in self.issues))


def _load_schema() -> dict[str, Any]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _validator() -> jsonschema.Draft202012Validator:
    schema = _load_schema()
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    return jsonschema.Draft202012Validator({"$ref": _SCHEMA_ID}, registry=registry)


def _json_path(parts: Any) -> str:
    values = [str(part) for part in parts]
    return "$" if not values else "$." + ".".join(values)


def load_catalog(module_dir: Path) -> dict[str, Any] | None:
    """Load api_catalog.json from a module directory, or return None if absent."""
    catalog_path = module_dir / _CATALOG_FILE
    if not catalog_path.exists():
        return None
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def validate_catalog_schema(catalog: dict[str, Any]) -> list[CatalogIssue]:
    """Return structural issues found in a catalog against the JSON Schema contract."""
    issues: list[CatalogIssue] = []
    errors = sorted(
        _validator().iter_errors(catalog),
        key=lambda error: (tuple(str(item) for item in error.absolute_path), error.message),
    )
    for error in errors:
        issues.append(
            CatalogIssue(
                path=_json_path(error.absolute_path),
                code="invalid_catalog_schema",
                message=error.message,
                suggested_repair="Update the value to satisfy the api-catalog schema.",
            )
        )
    return sorted(issues)


def catalog_default_selection(catalog: dict[str, Any]) -> dict[str, list[str]]:
    """Return the tier-core default selection resolved FROM the catalog.

    A ``catalog_driven`` job that carries no explicit ``selection`` falls back to
    this default: every non-excluded catalog field whose ``tier`` is ``core``,
    partitioned into metrics/dimensions by ``kind``. This keeps the default
    honest — it is the catalog's own core tier, never a hardcoded field list
    (Jean: the catalog IS the execution contract).

    Pure function; performs no I/O. Deterministic (sorted) output.
    """
    metrics: list[str] = []
    dimensions: list[str] = []
    for field in catalog.get("fields", []):
        field_id = field.get("field_id")
        if not field_id:
            continue
        if field.get("exposure") == "excluded":
            continue
        if field.get("tier") != "core":
            continue
        if field.get("kind") == "metric":
            metrics.append(field_id)
        elif field.get("kind") == "dimension":
            dimensions.append(field_id)
    return {"metrics": sorted(metrics), "dimensions": sorted(dimensions)}


def validate_selection(
    catalog: dict[str, Any],
    selection: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[CatalogIssue]]:
    """Validate a field *selection* against a module's API *catalog* (AD-2, generic).

    The selection is ``{"metrics": [...], "dimensions": [...]}``. Every id must
    exist in the catalog's ``fields`` (the drift signal — an unknown id is NEVER
    silently dropped; it becomes a typed refusal listing the offending ids).

    Returns ``(resolved, issues)``:
      - ``resolved`` = ``{"metrics": [...], "dimensions": [...],
        "source_fields": {field_id: source_field}}`` resolved from the catalog.
        On any issue, ``source_fields`` covers only the fields that DID resolve
        (callers must not extract a partial selection when ``issues`` is
        non-empty — they refuse).
      - ``issues`` = deterministic list of :class:`CatalogIssue`; empty when the
        whole selection is known.

    This function contains no provider vocabulary; the catalog carries all
    provider knowledge. A ``None`` selection resolves to the tier-core default.
    """
    if selection is None:
        selection = catalog_default_selection(catalog)

    catalog_fields: dict[str, dict[str, Any]] = {
        field["field_id"]: field
        for field in catalog.get("fields", [])
        if field.get("field_id")
    }

    requested_metrics = list(selection.get("metrics", []) or [])
    requested_dimensions = list(selection.get("dimensions", []) or [])

    issues: list[CatalogIssue] = []
    source_fields: dict[str, str] = {}
    unknown: list[str] = []

    for kind, requested in (("metric", requested_metrics), ("dimension", requested_dimensions)):
        for field_id in requested:
            entry = catalog_fields.get(field_id)
            if entry is None:
                unknown.append(field_id)
                continue
            source_fields[field_id] = entry.get("source_field", field_id)

    if unknown:
        issues.append(
            CatalogIssue(
                path="$.selection",
                code="unknown_selection_field",
                message=(
                    "Selection references field id(s) absent from the catalog: "
                    + ", ".join(sorted(set(unknown)))
                    + " (catalog drift — the catalog is the execution contract)."
                ),
                suggested_repair=(
                    "Remove the unknown field id(s) from the selection, or regenerate "
                    "the api_catalog.json so it declares them."
                ),
            )
        )

    resolved = {
        "metrics": requested_metrics,
        "dimensions": requested_dimensions,
        "source_fields": source_fields,
    }
    return resolved, sorted(issues)


def diff_catalog_manifest(
    catalog: dict[str, Any],
    manifest: dict[str, Any],
) -> list[CatalogIssue]:
    """Compare a catalog against a manifest and return deterministic issues.

    Pure function — performs no I/O.

    The catalog is the source-side truth (what the provider API exposes).
    The manifest is the platform-side projection (what the platform surfaces).

    Invariants checked:
    - Every catalog field with exposure=exposed must appear in the manifest.
    - Every manifest field must have a catalog entry.
    - Every catalog field with exposure=excluded must carry an exclusion_reason.
    - kind must agree between catalog and manifest when both declare a field.
    - source_field must agree between catalog and manifest when both declare a field.
    - Every catalog field must carry a tier value (already enforced by schema,
      but cross-checked here for consistency with the manifest diff surface).
    """
    issues: list[CatalogIssue] = []

    # .get("field_id") + None filter: a malformed manifest entry without a
    # field_id must not crash the diff with a KeyError (the manifest side is
    # validated by the source-capabilities gate, not by this function).
    catalog_fields: dict[str, dict[str, Any]] = {
        field["field_id"]: field
        for field in catalog.get("fields", [])
        if field.get("field_id")
    }

    source_caps = manifest.get("source_capabilities", {})
    manifest_fields: dict[str, dict[str, Any]] = {
        field["field_id"]: field
        for field in source_caps.get("fields", [])
        if field.get("field_id")
    }

    # Story 25.8: when the module declares a catalog_driven report profile, the
    # projection is DYNAMIC (any cataloged field is selectable at execution) --
    # exposed fields no longer need to be enumerated in the manifest. The
    # manifest->catalog direction (no phantom fields) still applies fully.
    has_catalog_driven = (
        catalog.get("exposure_policy") == "catalog_driven"
        or any(
            report.get("selection_mode") == "catalog_driven"
            for report in source_caps.get("reports", [])
        )
    )

    # catalog_field_not_exposed: exposed catalog field absent from manifest
    # (only meaningful for pure exact_bundle modules).
    for field_id, cat_field in sorted(catalog_fields.items()):
        if has_catalog_driven:
            break
        exposure = cat_field.get("exposure")
        if exposure == "exposed" and field_id not in manifest_fields:
            issues.append(
                CatalogIssue(
                    path=f"$.source_capabilities.fields.{field_id}",
                    code="catalog_field_not_exposed",
                    message=(
                        f"Catalog field {field_id!r} has exposure=exposed "
                        "but is absent from the manifest."
                    ),
                    suggested_repair=(
                        "Add the field to source_capabilities.fields in the manifest, "
                        "or change its catalog exposure to planned/excluded."
                    ),
                )
            )

    # manifest_field_not_in_catalog: manifest field with no catalog entry
    for field_id in sorted(manifest_fields):
        if field_id not in catalog_fields:
            issues.append(
                CatalogIssue(
                    path=f"$.source_capabilities.fields.{field_id}",
                    code="manifest_field_not_in_catalog",
                    message=(
                        f"Manifest field {field_id!r} has no corresponding entry "
                        "in the API catalog."
                    ),
                    suggested_repair=(
                        "Add the field to the catalog with its correct exposure "
                        "and metadata, or remove it from the manifest."
                    ),
                )
            )

    # Cross-field checks for fields present in both
    for field_id in sorted(set(catalog_fields) & set(manifest_fields)):
        cat_field = catalog_fields[field_id]
        man_field = manifest_fields[field_id]
        field_path = f"$.source_capabilities.fields.{field_id}"

        # excluded_without_reason: catalog field is excluded but has no reason
        if cat_field.get("exposure") == "excluded" and not cat_field.get("exclusion_reason"):
            issues.append(
                CatalogIssue(
                    path=f"$.fields.{field_id}.exclusion_reason",
                    code="excluded_without_reason",
                    message=(
                        f"Catalog field {field_id!r} has exposure=excluded "
                        "but no exclusion_reason is provided."
                    ),
                    suggested_repair=(
                        "Add an exclusion_reason explaining why this field is excluded."
                    ),
                )
            )

        # kind_mismatch: kind disagrees between catalog and manifest
        cat_kind = cat_field.get("kind")
        man_kind = man_field.get("kind")
        if cat_kind and man_kind and cat_kind != man_kind:
            issues.append(
                CatalogIssue(
                    path=f"{field_path}.kind",
                    code="kind_mismatch",
                    message=(
                        f"Field {field_id!r} is kind={cat_kind!r} in the catalog "
                        f"but kind={man_kind!r} in the manifest."
                    ),
                    suggested_repair=(
                        "Align the kind in the manifest with the catalog definition."
                    ),
                )
            )

        # source_field_mismatch: source_field disagrees between catalog and manifest
        cat_source = cat_field.get("source_field")
        man_source = man_field.get("source_field")
        if cat_source and man_source and cat_source != man_source:
            issues.append(
                CatalogIssue(
                    path=f"{field_path}.source_field",
                    code="source_field_mismatch",
                    message=(
                        f"Field {field_id!r} has source_field={cat_source!r} in the catalog "
                        f"but source_field={man_source!r} in the manifest."
                    ),
                    suggested_repair=(
                        "Align the source_field in the manifest with the catalog definition."
                    ),
                )
            )

        # tier_missing: catalog field lacks a tier value
        if not cat_field.get("tier"):
            issues.append(
                CatalogIssue(
                    path=f"$.fields.{field_id}.tier",
                    code="tier_missing",
                    message=(
                        f"Catalog field {field_id!r} is missing a tier value."
                    ),
                    suggested_repair=(
                        "Set tier to one of: core, standard, advanced."
                    ),
                )
            )

    # Also check excluded_without_reason for catalog-only fields
    for field_id in sorted(set(catalog_fields) - set(manifest_fields)):
        cat_field = catalog_fields[field_id]
        if cat_field.get("exposure") == "excluded" and not cat_field.get("exclusion_reason"):
            issues.append(
                CatalogIssue(
                    path=f"$.fields.{field_id}.exclusion_reason",
                    code="excluded_without_reason",
                    message=(
                        f"Catalog field {field_id!r} has exposure=excluded "
                        "but no exclusion_reason is provided."
                    ),
                    suggested_repair=(
                        "Add an exclusion_reason explaining why this field is excluded."
                    ),
                )
            )
        # tier_missing for catalog-only fields
        if not cat_field.get("tier"):
            issues.append(
                CatalogIssue(
                    path=f"$.fields.{field_id}.tier",
                    code="tier_missing",
                    message=(
                        f"Catalog field {field_id!r} is missing a tier value."
                    ),
                    suggested_repair=(
                        "Set tier to one of: core, standard, advanced."
                    ),
                )
            )

    return sorted(issues)
