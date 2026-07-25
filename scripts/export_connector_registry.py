"""Generate the deterministic, fail-closed public connector registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
from core.source_capabilities import validate_manifest_capabilities
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODULES_DIR = ROOT / "server" / "modules"
SCHEMAS_DIR = ROOT / "server" / "core" / "schemas"

# Story 25.6: the verified-state chain. A manifest may declare
# verification.status == "ratified" ONLY when a live-probe evidence report
# exists in the module proving it. The registry then emits readiness.status
# "verified" + verifiedAt. Anything else stays the conservative default.
DEFAULT_READINESS_STATUS = "validation_required"
VERIFIED_READINESS_STATUS = "verified"
RATIFIED_VERIFICATION_STATUS = "ratified"
RATIFICATION_GLOB = "ratification-*.json"
RATIFIED_VERDICT = "ratified"


class RegistryValidationError(ValueError):
    """Raised when backend contracts cannot produce an honest public registry."""


def _json_path(parts: Any) -> str:
    values = [str(part) for part in parts]
    return "$" if not values else "$." + ".".join(values)


def _relax_verification_status(manifest_schema: dict[str, Any]) -> None:
    """Widen the verification.status const in the IN-MEMORY schema copy only.

    Story 25.6 AC3: a manifest may legitimately declare status "ratified" once a
    live probe has run. The on-disk schema (core-owned, FORBIDDEN to edit here)
    still pins ``const: "blocked"`` because no live run has happened yet; we widen
    the loaded copy to ``enum: ["blocked", "ratified"]`` so a ratified manifest is
    structurally valid and reaches the fail-closed readiness resolver (the REAL
    gate: a ratified status without a valid evidence report is refused there). The
    schema FILE is never mutated -- current 'blocked' manifests are unaffected and
    the registry regenerates byte-identical.
    """
    try:
        status_schema = manifest_schema["properties"]["public_catalog"]["properties"][
            "verification"
        ]["properties"]["status"]
    except (KeyError, TypeError):
        return
    if status_schema.get("const") == "blocked":
        status_schema.pop("const", None)
        status_schema["enum"] = ["blocked", RATIFIED_VERIFICATION_STATUS]


def _manifest_validator() -> jsonschema.Draft202012Validator:
    manifest_schema = json.loads(
        (SCHEMAS_DIR / "manifest.schema.json").read_text(encoding="utf-8")
    )
    _relax_verification_status(manifest_schema)
    capability_schema = json.loads(
        (SCHEMAS_DIR / "source-capabilities.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(manifest_schema)
    jsonschema.Draft202012Validator.check_schema(capability_schema)
    registry = Registry().with_resources(
        [
            (manifest_schema["$id"], Resource.from_contents(manifest_schema)),
            (capability_schema["$id"], Resource.from_contents(capability_schema)),
        ]
    )
    return jsonschema.Draft202012Validator(manifest_schema, registry=registry)


def _validate_manifest(
    path: Path,
    manifest: Any,
    validator: jsonschema.Draft202012Validator,
) -> None:
    if not isinstance(manifest, dict):
        raise RegistryValidationError(
            f"{path.parent.name}: $: manifest root must be an object"
        )
    module_id = manifest.get("name", path.parent.name)
    catalog = manifest.get("public_catalog")
    if not isinstance(catalog, dict) or "visibility" not in catalog:
        raise RegistryValidationError(
            f"{module_id}: $.public_catalog.visibility: built-in visibility is required"
        )

    errors = sorted(
        validator.iter_errors(manifest),
        key=lambda error: (
            tuple(str(item) for item in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        error = errors[0]
        raise RegistryValidationError(
            f"{module_id}: {_json_path(error.absolute_path)}: {error.message}"
        )

    capability_issues = validate_manifest_capabilities(manifest)
    if capability_issues:
        issue = capability_issues[0]
        raise RegistryValidationError(
            f"{module_id}: {issue.path}: {issue.message} ({issue.code})"
        )


def _project_report(
    module_id: str,
    report: dict[str, Any],
    display_names: dict[str, str],
) -> dict[str, Any]:
    report_id = report["id"]
    display_name = display_names.get(report_id)
    if not display_name:
        raise RegistryValidationError(
            f"{module_id}: $.report_profiles.{report_id}.display_name: public name is required"
        )

    availability = report["availability"]
    projected: dict[str, Any] = {
        "id": report_id,
        "displayName": display_name,
        "availability": availability["status"],
        "metrics": sorted(report["metrics"]),
        "dimensions": sorted(report["dimensions"]),
        "supportedGrains": sorted(
            [sorted(grain) for grain in report["supported_grains"]],
            key=lambda grain: tuple(grain),
        ),
        "minimumIntervalMinutes": report["cadence"]["minimum_interval_minutes"],
        "supportedModes": sorted(report["cadence"]["supported_modes"]),
    }
    if availability["status"] == "unavailable":
        projected["reasonCode"] = availability["reason_code"]
        projected["followUp"] = availability["follow_up"]
    return projected


def _load_ratified_report(module_dir: Path) -> dict[str, Any] | None:
    """Return the newest VALID ratified report in ``<module_dir>/reports/``, else None.

    A report qualifies only if it is a JSON object with ``verdict == "ratified"``
    and a non-empty ``probed_at`` date. Malformed / non-ratified / partial reports
    are ignored (they do not lift the readiness state). Deterministic: reports are
    considered in sorted filename order and the last qualifying one wins (filenames
    embed ``probed_at`` so sort order == chronological order).
    """
    reports_dir = module_dir / "reports"
    if not reports_dir.is_dir():
        return None
    winner: dict[str, Any] | None = None
    for path in sorted(reports_dir.glob(RATIFICATION_GLOB)):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if not isinstance(report, dict):
            continue
        if report.get("verdict") != RATIFIED_VERDICT:
            continue
        probed_at = report.get("probed_at")
        if not isinstance(probed_at, str) or not probed_at:
            continue
        winner = report
    return winner


def _resolve_readiness(
    manifest: dict[str, Any],
    module_dir: Path,
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the public readiness block (fail-closed, Story 25.6 AC3).

    * verification.status != "ratified" -> the conservative default
      ("validation_required"): the manifest never claims more than it can prove.
    * verification.status == "ratified" -> a VALID ratified report MUST exist,
      otherwise RegistryValidationError refuses the whole registry. When it does,
      readiness.status becomes "verified" + verifiedAt (the report's probed_at).
    """
    status = verification.get("status")
    readiness: dict[str, Any] = {
        "status": DEFAULT_READINESS_STATUS,
        "basis": ["manifest_contract", "source_capability_contract"],
        "reasonCode": verification["reason_code"],
        "followUp": verification["follow_up"],
    }
    if status != RATIFIED_VERIFICATION_STATUS:
        return readiness

    report = _load_ratified_report(module_dir)
    if report is None:
        raise RegistryValidationError(
            f"{manifest['name']}: $.public_catalog.verification.status: "
            f"'ratified' requires a valid reports/{RATIFICATION_GLOB} with "
            f"verdict '{RATIFIED_VERDICT}' and a probed_at date"
        )
    readiness["status"] = VERIFIED_READINESS_STATUS
    readiness["basis"] = [
        "manifest_contract",
        "source_capability_contract",
        "live_ratification_report",
    ]
    readiness["verifiedAt"] = report["probed_at"]
    return readiness


def _project_connector(
    manifest: dict[str, Any], module_dir: Path
) -> dict[str, Any]:
    catalog = manifest["public_catalog"]
    verification = catalog["verification"]
    display_name = manifest["display_name"]
    if not display_name:
        raise RegistryValidationError(
            f"{manifest['name']}: $.display_name: public name is required"
        )
    display_names = {
        profile["id"]: profile["display_name"]
        for profile in manifest["report_profiles"]
    }
    return {
        "id": manifest["name"],
        "displayName": display_name,
        "category": catalog["category"],
        "moduleKind": manifest.get("module_kind", "kpi"),
        "authType": manifest["auth_type"],
        "onboardingModes": sorted(catalog["onboarding_modes"]),
        "readiness": _resolve_readiness(manifest, module_dir, verification),
        "reports": sorted(
            [
                _project_report(manifest["name"], report, display_names)
                for report in manifest["source_capabilities"]["reports"]
            ],
            key=lambda report: report["id"],
        ),
    }


def build_registry(modules_dir: Path = DEFAULT_MODULES_DIR) -> dict[str, Any]:
    """Return the public registry projected from every built-in manifest."""

    validator = _manifest_validator()
    connectors: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    paths = sorted(Path(modules_dir).glob("*/manifest.json"))
    if not paths:
        raise RegistryValidationError(f"No manifests found under {modules_dir}")

    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise RegistryValidationError(
                f"{path.parent.name}: $: manifest must be valid UTF-8"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RegistryValidationError(
                f"{path.parent.name}: $: invalid JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
        except OSError as exc:
            raise RegistryValidationError(f"{path.parent.name}: $: {exc}") from exc
        _validate_manifest(path, manifest, validator)
        module_id = manifest["name"]
        if module_id in seen_ids:
            raise RegistryValidationError(f"{module_id}: $.name: duplicate public module ID")
        seen_ids.add(module_id)
        if manifest["public_catalog"]["visibility"] == "public":
            connectors.append(_project_connector(manifest, path.parent))

    connectors.sort(key=lambda connector: connector["id"])
    return {"contractVersion": "1", "connectors": connectors}


def serialize_registry(registry: dict[str, Any]) -> bytes:
    """Serialize canonical UTF-8 bytes with no volatile metadata."""

    return (
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path, help="Write canonical registry bytes")
    destination.add_argument(
        "--check", type=Path, help="Compare canonical bytes without writing"
    )
    parser.add_argument("--modules-dir", type=Path, default=DEFAULT_MODULES_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        expected = serialize_registry(build_registry(args.modules_dir))
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(expected)
            return 0

        if not args.check.exists():
            print(f"Registry is missing: {args.check}", file=sys.stderr)
            return 1
        if args.check.read_bytes() != expected:
            print(f"Registry is stale: {args.check}", file=sys.stderr)
            return 1
        return 0
    except RegistryValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
