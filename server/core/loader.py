"""toorow module loader.

Story 1.3 — Module Loader & Contract v1.
Story 3.1 — Airbyte extraction path routing.

Implements auto-discovery of connector modules from the ``server/modules/``
directory. Each module folder must contain:
  - ``manifest.json`` — validated against the published manifest schema v1
  - ``connector.py``  — exposes a ``mcp_app: FastMCP`` attribute

# AD-2: loader is source-agnostic
The loader contains zero references to any specific module name. It operates
only on the generic module contract defined by the manifest schema.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import jsonschema
from referencing import Registry, Resource

from core.source_capabilities import CapabilityIssue, validate_manifest_capabilities

logger = logging.getLogger(__name__)

# Path to the manifest JSON Schema (co-located with this module in core/schemas/).
_SCHEMA_PATH = Path(__file__).parent / "schemas" / "manifest.schema.json"
_CAPABILITY_SCHEMA_PATH = Path(__file__).parent / "schemas" / "source-capabilities.schema.json"

# Path to the report-definition JSON Schema (Story 6.1, AC1).
_REPORT_SCHEMA_PATH = Path(__file__).parent / "schemas" / "report.schema.json"

# Lazy-loaded schema dicts — loaded once and reused for all validations.
_manifest_schema: dict | None = None
_capability_schema: dict | None = None
_report_schema: dict | None = None


def _get_manifest_schema() -> dict:
    """Load and cache the manifest JSON Schema from disk."""
    global _manifest_schema
    if _manifest_schema is None:
        _manifest_schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _manifest_schema


def _get_capability_schema() -> dict:
    """Load and cache the canonical source-capability schema."""
    global _capability_schema
    if _capability_schema is None:
        _capability_schema = json.loads(_CAPABILITY_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(_capability_schema)
    return _capability_schema


def _get_manifest_validator() -> jsonschema.Draft202012Validator:
    """Build an offline validator with the canonical capability resource registered."""
    capability_schema = _get_capability_schema()
    registry = Registry().with_resource(
        capability_schema["$id"], Resource.from_contents(capability_schema)
    )
    return jsonschema.Draft202012Validator(_get_manifest_schema(), registry=registry)


def _requires_capabilities(schema_version: object) -> bool:
    """Return whether a manifest version is on the strict capability contract."""
    try:
        major, *minor = (int(part) for part in str(schema_version).split("."))
    except (TypeError, ValueError):
        return False
    return major == 1 and (minor[0] if minor else 0) >= 2


def _manifest_schema_issues(
    validator: jsonschema.Draft202012Validator, manifest: dict
) -> list[dict[str, str]]:
    errors = sorted(
        validator.iter_errors(manifest),
        key=lambda error: (tuple(str(item) for item in error.absolute_path), error.message),
    )
    return [
        {
            "code": "invalid_manifest_schema",
            "path": "$"
            if not error.absolute_path
            else "$." + ".".join(str(item) for item in error.absolute_path),
            "message": error.message,
            "suggested_repair": "Update manifest.json to satisfy the versioned manifest schema.",
        }
        for error in errors
    ]


def _dispatch_issues(manifest: dict, connector_module: ModuleType) -> list[CapabilityIssue]:
    """Validate selectable callables against the synchronous queue contract."""

    issues: list[CapabilityIssue] = []
    queue_kwargs = {
        "connection_id": "",
        "date_from": "",
        "date_to": "",
        "project_id": "",
        "pull_id": "",
    }
    for index, report in enumerate(manifest["source_capabilities"]["reports"]):
        if report["availability"]["status"] != "selectable":
            continue
        path = f"$.source_capabilities.reports.{index}.dispatch.callable"
        callable_name = report["dispatch"]["callable"]
        dispatch_fn = getattr(connector_module, callable_name, None)
        if not callable(dispatch_fn):
            issues.append(
                CapabilityIssue(
                    path=path,
                    code="dispatch_callable_missing",
                    message=f"Declared callable {callable_name!r} is not exposed by connector.py.",
                    suggested_repair=(
                        "Expose the proven callable or mark the profile unavailable "
                        "with a follow-up."
                    ),
                )
            )
            continue
        if inspect.iscoroutinefunction(dispatch_fn):
            issues.append(
                CapabilityIssue(
                    path=path,
                    code="dispatch_callable_async",
                    message=f"Declared callable {callable_name!r} is asynchronous.",
                    suggested_repair=(
                        "Expose a synchronous queue adapter or mark the profile unavailable."
                    ),
                )
            )
            continue
        try:
            inspect.signature(dispatch_fn).bind(**queue_kwargs)
        except TypeError as exc:
            issues.append(
                CapabilityIssue(
                    path=path,
                    code="dispatch_signature_incompatible",
                    message=(
                        f"Declared callable {callable_name!r} cannot accept the queue "
                        f"contract: {exc}."
                    ),
                    suggested_repair=(
                        "Expose a callable accepting connection_id, date_from, date_to, "
                        "project_id, and pull_id, or mark the profile unavailable."
                    ),
                )
            )
    return sorted(issues)


def _get_report_schema() -> dict:
    """Load and cache the report-definition JSON Schema from disk (Story 6.1)."""
    global _report_schema
    if _report_schema is None:
        _report_schema = json.loads(_REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _report_schema


@dataclass
class LoadedModule:
    """Represents a successfully loaded connector module.

        Attributes:
            name: The module folder name (kebab-case, from the manifest ``name`` field).
            manifest: The raw validated manifest dict.
            connector_module: The imported Python module exposing ``mcp_app: FastMCP``.
            field_compat_rules: The module's VALIDATED ``field_compatibility``
                rules block (Story 26.1 B, review F-10), or None when the
                module declares none. Consumers (pre-call enforcement) read
                the rules here instead of re-parsing catalog_sources.json.
    """

    name: str
    manifest: dict
    connector_module: ModuleType
    reports: list[dict] = field(default_factory=list)
    capabilities_available: bool = False
    field_compat_rules: dict | None = None


def _discover_reports(module_dir: Path, module_name: str) -> list[dict]:
    """Scan ``<module_dir>/reports/*.json`` and return valid report definitions.

    Story 6.1 (AC2). Each ``*.json`` is validated in two stages:
      1. JSON Schema validation against ``report.schema.json`` (structural).
      2. Semantic validation of ``metrics[]`` / ``dimensions[]`` against the
         canonical dbt dictionary (core.report_dictionary).

    Resilience rule (AC2.6): an invalid report is logged as a WARNING and
    SKIPPED — it never crashes the module or the server. A module with no
    ``reports/`` folder or an empty one loads normally with ``reports == []``
    (AC2.7).

    # AD-2: source-agnostic — no module-specific strings; the module name is
    #       passed in purely for log context.
    """
    from core import report_dictionary  # noqa: PLC0415

    reports_dir = module_dir / "reports"
    valid: list[dict] = []
    if not reports_dir.is_dir():
        return valid

    schema = _get_report_schema()
    validator = jsonschema.Draft7Validator(schema)

    for report_path in sorted(reports_dir.glob("*.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "report_skipped",
                        "module": module_name,
                        "file": report_path.name,
                        "errors": [f"invalid JSON: {exc}"],
                    }
                )
            )
            continue

        # Stage 1 — JSON Schema validation.
        schema_errors = [
            f"{list(e.path) or 'root'}: {e.message}" for e in validator.iter_errors(report)
        ]
        if schema_errors:
            logger.warning(
                json.dumps(
                    {
                        "event": "report_skipped",
                        "module": module_name,
                        "file": report_path.name,
                        "errors": schema_errors,
                    }
                )
            )
            continue

        # Stage 2 — canonical metric/dimension dictionary validation (AC2.3/2.4).
        dict_errors: list[str] = []
        for metric in report.get("metrics", []):
            if not report_dictionary.is_known_metric(metric):
                dict_errors.append(f"unknown metric: {metric!r}")
        for dimension in report.get("dimensions", []):
            if not report_dictionary.is_known_dimension(dimension):
                dict_errors.append(f"unknown dimension: {dimension!r}")
        if dict_errors:
            logger.warning(
                json.dumps(
                    {
                        "event": "report_skipped",
                        "module": module_name,
                        "file": report_path.name,
                        "errors": dict_errors,
                    }
                )
            )
            continue

        valid.append(report)
        logger.info(
            json.dumps(
                {
                    "event": "report_loaded",
                    "module": module_name,
                    "report_id": report.get("id"),
                }
            )
        )

    return valid


def _load_connector_module(module_dir: Path) -> ModuleType:
    """Dynamically import ``connector.py`` from a module directory.

    Uses ``importlib.util.spec_from_file_location`` so the module is isolated
    under the synthetic name ``connector_<folder-name>`` — avoids import naming
    collisions when multiple modules are loaded.

    Args:
        module_dir: Path to the module folder containing ``connector.py``.

    Returns:
        The loaded Python module.

    Raises:
        ImportError: If ``connector.py`` cannot be loaded.
    """
    connector_path = module_dir / "connector.py"
    module_name = f"connector_{module_dir.name.replace('-', '_')}"

    spec = importlib.util.spec_from_file_location(module_name, connector_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec from {connector_path}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except BaseException:
        # Review-1-3 M2: never leave a half-initialised module in sys.modules —
        # a later import of the same name would silently get the broken object.
        sys.modules.pop(module_name, None)
        raise
    return mod


def scan_and_load_modules(modules_dir: Path) -> list[LoadedModule]:
    """Scan ``modules_dir`` and load all valid connector modules.

    For each immediate subdirectory of ``modules_dir``:
      1. Read ``manifest.json`` and validate against manifest schema v1.
      2. On validation failure: log structured error and skip (do not crash).
      3. Import ``connector.py`` and verify ``mcp_app`` attribute.
      4. On import failure or missing ``mcp_app``: log structured error and skip.

    Non-directory entries (files, symlinks to files, etc.) are silently ignored.

    # AD-2: loader is source-agnostic — no module-specific identifiers here.

    Args:
        modules_dir: Absolute or relative path to the modules directory.
                     The function resolves it relative to cwd if relative.

    Returns:
        List of :class:`LoadedModule` for every successfully loaded module.
        Modules that fail validation or import are omitted; the server continues.
    """
    modules_dir = Path(modules_dir)
    validator = _get_manifest_validator()
    loaded: list[LoadedModule] = []

    if not modules_dir.is_dir():
        logger.warning(
            json.dumps(
                {
                    "event": "modules_dir_missing",
                    "path": str(modules_dir),
                    "message": "modules directory does not exist; no modules loaded",
                }
            )
        )
        return loaded

    for entry in sorted(modules_dir.iterdir()):
        # Non-directory entries (e.g. .gitkeep files) are silently skipped.
        if not entry.is_dir():
            continue

        folder_name = entry.name

        # ------------------------------------------------------------------ #
        # Step 1 — Read and validate manifest.json                            #
        # ------------------------------------------------------------------ #
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            logger.warning(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": ["manifest.json not found"],
                    }
                )
            )
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": [f"manifest.json is not valid JSON: {exc}"],
                    }
                )
            )
            continue

        validation_errors = _manifest_schema_issues(validator, manifest)
        if validation_errors:
            logger.error(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": validation_errors,
                    }
                )
            )
            continue

        capabilities_available = _requires_capabilities(manifest.get("schema_version"))
        if capabilities_available:
            capability_errors = validate_manifest_capabilities(manifest)
            if capability_errors:
                logger.error(
                    json.dumps(
                        {
                            "event": "module_skipped",
                            "module": folder_name,
                            "errors": [error.as_dict() for error in capability_errors],
                        }
                    )
                )
                continue
        else:
            logger.warning(
                json.dumps(
                    {
                        "event": "module_capabilities_unavailable",
                        "module": folder_name,
                        "code": "legacy_capabilities_unavailable",
                        "path": "$.schema_version",
                        "message": (
                            "Legacy module loaded for execution but excluded from "
                            "the capability catalog."
                        ),
                        "suggested_repair": "Migrate the manifest to schema version 1.2.",
                    }
                )
            )

        # ------------------------------------------------------------------ #
        # Step 1b — Validate field_compatibility rules (Story 26.1, B).       #
        # Modules without a catalog_sources.json or without the block are     #
        # untouched (None -> no-op). Malformed rules skip the module LOUDLY   #
        # -- a broken compatibility contract must never load as valid.        #
        # Review 26.1 F-10: the parsed rules are ATTACHED to the             #
        # LoadedModule (field_compat_rules) so enforcement reads them from    #
        # the registry instead of re-parsing catalog_sources.json.            #
        # ------------------------------------------------------------------ #
        from core import field_compat  # noqa: PLC0415

        try:
            field_compat_rules = field_compat.load_module_rules(entry)
        except field_compat.FieldCompatibilityError as exc:
            logger.error(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": [f"field_compatibility_invalid: {err}" for err in exc.errors],
                    }
                )
            )
            continue

        # ------------------------------------------------------------------ #
        # Step 2 — Import connector.py and verify mcp_app attribute           #
        # ------------------------------------------------------------------ #
        connector_path = entry / "connector.py"
        if not connector_path.is_file():
            logger.error(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": ["connector.py not found"],
                    }
                )
            )
            continue

        try:
            mod = _load_connector_module(entry)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": [f"connector_load_failed: {exc}"],
                    }
                )
            )
            continue

        if not hasattr(mod, "mcp_app"):
            logger.error(
                json.dumps(
                    {
                        "event": "module_skipped",
                        "module": folder_name,
                        "errors": [
                            "connector.py must expose a module-level `mcp_app: FastMCP` attribute"
                        ],
                    }
                )
            )
            continue

        if capabilities_available:
            dispatch_errors = _dispatch_issues(manifest, mod)
            if dispatch_errors:
                logger.error(
                    json.dumps(
                        {
                            "event": "module_skipped",
                            "module": folder_name,
                            "errors": [error.as_dict() for error in dispatch_errors],
                        }
                    )
                )
                sys.modules.pop(mod.__name__, None)
                continue

        module_name = manifest.get("name", folder_name)

        # ------------------------------------------------------------------ #
        # Step 3 — Register quota config from manifest (AC6, Story 3.3)       #
        # ------------------------------------------------------------------ #
        quota_block = manifest.get("quota")
        if quota_block is not None:
            _required_quota_keys = {"window_seconds", "budget_points", "read_cost", "write_cost"}
            if not isinstance(quota_block, dict) or not _required_quota_keys.issubset(quota_block):
                logger.warning(
                    "loader: manifest %s has invalid quota block -- skipping quota registration",
                    module_name,
                )
            else:
                try:
                    from core import quota as _quota  # noqa: PLC0415

                    _quota.register_platform(
                        module_name,
                        window_seconds=int(quota_block["window_seconds"]),
                        budget_points=int(quota_block["budget_points"]),
                        read_cost=int(quota_block["read_cost"]),
                        write_cost=int(quota_block["write_cost"]),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("loader: quota registration failed for %s: %s", module_name, exc)

        # ------------------------------------------------------------------ #
        # Step 4 — Discover + validate report definitions (Story 6.1, AC2)   #
        # A module with an invalid report has it SKIPPED (WARNING), not the   #
        # whole module; an empty/absent reports/ folder loads with reports=[].#
        #                                                                     #
        # Register this module's declared breakdown dimensions (canonical     #
        # values of canonical_dimension_mapping) into the report dictionary   #
        # first, so a report's dimensions[] validate against the full         #
        # warehouse vocabulary (AD-2: no connector name is hard-coded).       #
        # ------------------------------------------------------------------ #
        try:
            from core import report_dictionary as _report_dict  # noqa: PLC0415

            _report_dict.register_dimensions(
                manifest.get("canonical_dimension_mapping", {}).values()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("loader: dimension registration failed for %s: %s", module_name, exc)

        reports = _discover_reports(entry, module_name)

        loaded.append(
            LoadedModule(
                name=module_name,
                manifest=manifest,
                connector_module=mod,
                reports=reports,
                capabilities_available=capabilities_available,
                field_compat_rules=field_compat_rules,
            )
        )
        logger.info(
            json.dumps(
                {
                    "event": "module_loaded",
                    "module": module_name,
                    "display_name": manifest.get("display_name"),
                }
            )
        )

    return loaded


# ---------------------------------------------------------------------------
# Story 3.1 -- Extraction path helpers (AC5, T6)
# ---------------------------------------------------------------------------

_EXTRACTION_PATH_DEFAULT = "custom_pull"


def _get_extraction_path(manifest: dict, profile_id: str) -> str:
    """Return the extraction_path for a given report profile ID.

    Reads ``extraction_path`` from the matched profile in ``manifest.report_profiles``.
    Falls back to ``"custom_pull"`` if the field is absent (backward-compatible default).

    Args:
        manifest:   The validated manifest dict (from LoadedModule.manifest).
        profile_id: The report profile ``id`` to match (e.g. ``"standard_daily"``).

    Returns:
        ``"custom_pull"`` or ``"airbyte_sync"``.
    """
    profiles: list[dict] = manifest.get("report_profiles", [])
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile.get("extraction_path", _EXTRACTION_PATH_DEFAULT)
    return _EXTRACTION_PATH_DEFAULT


def dispatch_pull(
    module_name: str,
    profile_id: str,
    *,
    loaded_modules: list,
    connection_id: str,
    airbyte_connection_id: str | None = None,
    date_from: str,
    date_to: str,
    project_id: str,
    pull_id: str,
    selection: dict | None = None,
) -> dict:
    """Route a pull dispatch based on the manifest's extraction_path for the profile.

    Story 25.8: when the resolved capability report's ``selection_mode`` is
    ``catalog_driven``, core loads the module's api_catalog.json, validates the
    supplied ``selection`` (absent -> the catalog's tier-core default), and
    passes ``selection=`` to the pull callable. exact_bundle/subset profiles are
    bit-identical: the callable is invoked with no ``selection=`` kwarg. A
    selection that references a field id absent from the catalog raises
    ``core.pull_errors.InvalidRequestError`` (the drift signal, never silent).

        Story 3.1 (AC5): reads extraction_path from the matched report profile and
        routes accordingly:
          - ``"custom_pull"``:   calls the module's ``pull()`` function (existing path).
          - ``"airbyte_sync"``:  triggers an Airbyte sync via AirbyteClient and returns
                                 a stub result. The actual data lands asynchronously.
                                 # TODO(Phase-B): poll / subscribe via webhook and integrate
                                 # with the queue's job tracking for end-to-end pull_id chain.

        The ``custom_pull`` route is unchanged and zero-risk for existing Story 2.7 and
        Story 3.2 queue flows -- no existing code path is modified.

        P3-dev note: the ``airbyte_sync`` route dispatches directly (not via the queue
        worker thread), because Airbyte manages its own job state. Phase B will wire the
        Airbyte sync result back through the core queue via a webhook or a Cloud Task
        completion callback.

        Args:
            module_name:          The provider/module name (e.g. the manifest ``name`` field).
            profile_id:           The report profile to dispatch (e.g. ``"standard_daily"``).
            loaded_modules:       The list of :class:`LoadedModule` from the loader registry.
            connection_id:        Nango connection ID (used for the custom_pull path).
            airbyte_connection_id: Airbyte connection UUID (used for the airbyte_sync path).
                                  If None and airbyte_sync is selected, raises ValueError.
            date_from:            ISO-8601 start date for custom_pull.
            date_to:              ISO-8601 end date for custom_pull.
            project_id:           Project identifier passed to pull().
            pull_id:              Core-minted pull_id (AD-7). For airbyte_sync rows the
                                  surrogate ``airbyte_<job_id>`` is used instead (AC4).

        Returns:
            For custom_pull: the result dict from ``pull()`` (unchanged contract).
            For airbyte_sync: ``{"pull_id": "airbyte_<job_id>", "row_count": None,
                                 "status": "dispatched"}``.

        Raises:
            ValueError:      If the module is not found, has no pull() function, or
                             airbyte_connection_id is missing for the airbyte_sync path.
            AirbyteSyncError (from core.airbyte_client): on Airbyte API errors.
    """
    loaded_module = next(
        (loaded for loaded in loaded_modules if loaded.name == module_name),
        None,
    )
    if loaded_module is None:
        raise ValueError(f"dispatch_pull: unknown module={module_name!r}")

    manifest = loaded_module.manifest
    legacy_profile = next(
        (
            profile
            for profile in manifest.get("report_profiles", [])
            if profile.get("id") == profile_id
        ),
        None,
    )
    if legacy_profile is None:
        raise ValueError(
            f"dispatch_pull: unknown profile={profile_id!r} for module={module_name!r}"
        )

    dispatch_fn = None
    descriptor = manifest.get("source_capabilities")
    if isinstance(descriptor, dict):
        report = next(
            (
                item
                for item in descriptor.get("reports", [])
                if item.get("id") == profile_id
            ),
            None,
        )
        if report is None:
            raise ValueError(
                f"dispatch_pull: unknown capability profile={profile_id!r} "
                f"for module={module_name!r}"
            )
        if report.get("availability", {}).get("status") != "selectable":
            raise ValueError(
                f"dispatch_pull: profile={profile_id!r} is unavailable "
                f"for module={module_name!r}"
            )
        callable_name = report.get("dispatch", {}).get("callable")
        dispatch_fn = getattr(loaded_module.connector_module, callable_name, None)
        if not callable(dispatch_fn):
            raise ValueError(
                f"dispatch_pull: declared callable={callable_name!r} is unavailable "
                f"for module={module_name!r}"
            )
    else:
        legacy_fn = getattr(loaded_module.connector_module, "pull", None)
        if callable(legacy_fn):
            dispatch_fn = legacy_fn

    extraction_path = legacy_profile.get(
        "extraction_path", _EXTRACTION_PATH_DEFAULT
    )

    if extraction_path == "airbyte_sync":
        if not airbyte_connection_id:
            raise ValueError(
                f"dispatch_pull: extraction_path='airbyte_sync' requires airbyte_connection_id "
                f"(module={module_name!r} profile={profile_id!r})"
            )
        from core.airbyte_client import trigger_sync  # noqa: PLC0415

        result = trigger_sync(airbyte_connection_id, pull_id=pull_id)
        job_id = result.get("job_id", "")
        # AC4: pull_id surrogate for Airbyte-originated rows (AD-7 exception documented)
        airbyte_pull_id = f"airbyte_{job_id}"
        logger.info(
            "loader: dispatch_pull airbyte_sync module=%s profile=%s airbyte_pull_id=%s",
            module_name,
            profile_id,
            airbyte_pull_id,
        )
        # TODO(Phase-B): integrate airbyte_pull_id with the queue's job tracking and
        # thread the core-minted pull_id through Airbyte job metadata (AD-7).
        return {"pull_id": airbyte_pull_id, "row_count": None, "status": "dispatched"}

    if dispatch_fn is None:
        raise ValueError(
            f"dispatch_pull: no callable found for module={module_name!r} "
            f"profile={profile_id!r}"
        )

    # Story 25.8: catalog_driven profiles resolve+validate a field selection from
    # the module's api_catalog.json and pass it to the callable as selection=.
    # Any other selection_mode (exact_bundle/subset) stays bit-identical.
    capability_report = None
    if isinstance(descriptor, dict):
        capability_report = next(
            (
                item
                for item in descriptor.get("reports", [])
                if item.get("id") == profile_id
            ),
            None,
        )
    if capability_report and capability_report.get("selection_mode") == "catalog_driven":
        from core.catalog_contract import load_catalog, validate_selection  # noqa: PLC0415
        from core.pull_errors import InvalidRequestError  # noqa: PLC0415

        module_dir = Path(__file__).parent.parent / "modules" / module_name
        catalog = load_catalog(module_dir)
        if catalog is None:
            raise InvalidRequestError(
                provider_status=None,
                provider_payload=None,
                message=(
                    f"catalog_driven profile requires api_catalog.json for module "
                    f"{module_name!r} but none was found"
                ),
            )
        resolved, issues = validate_selection(catalog, selection)
        if issues:
            raise InvalidRequestError(
                provider_status=None,
                provider_payload=None,
                message="; ".join(issue.message for issue in issues),
            )
        return dispatch_fn(
            connection_id=connection_id,
            date_from=date_from,
            date_to=date_to,
            project_id=project_id,
            pull_id=pull_id,
            selection=resolved,
        )

    return dispatch_fn(
        connection_id=connection_id,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        pull_id=pull_id,
    )
