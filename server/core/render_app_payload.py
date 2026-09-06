"""The one server-owned RenderInput and MCP render payload contract (Story 65.1)."""

from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

APP_PAYLOAD_SCHEMA_VERSION = 1
APP_PAYLOAD_KIND = "render"
APP_PAYLOAD_META_KEY = "toorow.app_payload"
MAX_RENDER_TOOL_META_BYTES = 262_144

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "render-app-payload.schema.json"
_PLACEHOLDER_PINS = frozenset({"", "legacy", "current", "deferred", "latest", "unknown", "none"})
_UNSAFE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "token",
        "password",
        "secret",
        "credentials",
        "api_key",
        "sql",
        "query_sql",
        "relation",
        "physical_relation",
        "warehouse_relation",
        "table_name",
        "dataset_id",
        "source_field",
        "source_id",
        "relation_ref",
        "option",
        "options",
        "series",
        "xaxis",
        "yaxis",
        "renderer_options",
    }
)
_SAFE_UNAVAILABLE_REASONS = frozenset(
    {
        "this Datastream has no published output to query yet",
        "the bound Datastream has no materialized output",
    }
)
_SAFE_UNAVAILABLE_REASON = "The source could not produce this result."
_SAFE_MISSING_LINK = "source_output"
_SAFE_RESULT_MANIFEST_KEYS = frozenset(
    {
        "grain",
        "time_window",
        "filters",
        "comparison",
        "truncated",
        "freshness",
        "semantic_view_version_id",
        "datastream_output_version_id",
        "missing_link",
        "unavailable_reason",
        "result_shape",
        "dq_evaluation_ids",
        "evidence_ids",
        # CHANTIER B. Both are disclosure, and withholding either would recreate
        # the defect: `grain_reconciliation` is the distance between a breakdown
        # and its declared total with the verdict and the declared reason, and
        # `chosen_by` says whether a declaration arbitrated between several
        # capable Datastreams or there was only one. A figure that cannot say
        # either is a figure whose provenance the reader has to guess.
        "grain_reconciliation",
        "chosen_by",
    }
)


def _project_safe_manifest_value(value: Any) -> Any:
    """Copy disclosed metadata while removing renderer/control-shaped keys."""
    if isinstance(value, dict):
        return {
            str(key): _project_safe_manifest_value(child)
            for key, child in value.items()
            if str(key).casefold() not in _UNSAFE_KEYS
        }
    if isinstance(value, list):
        return [_project_safe_manifest_value(child) for child in value]
    return deepcopy(value)


def _safe_unavailable_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if value in _SAFE_UNAVAILABLE_REASONS else _SAFE_UNAVAILABLE_REASON


def _safe_missing_link(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= 64 and "a" <= value[0] <= "z" and all(
        "a" <= char <= "z" or "0" <= char <= "9" or char == "_" for char in value
    ):
        return value
    return _SAFE_MISSING_LINK


_SAFE_PROVENANCE_KEYS = frozenset(
    {"datastream_id", "mapping_version_id", "pull_id", "publication_log_id"}
)
_SAFE_VALUE_PROVENANCE_KEYS = frozenset({"member_id", "pull_id"})
_BUSINESS_ROW_COLLECTION_PATH_SUFFIXES = (
    ".render_input.result.rows",
    ".toorow.result.rows",
    ".toorow.result.initial_projection",
    ".moved.rows",
    ".moved.initial_projection",
)

#: The ONE node whose keys are WELL NAMES of the ratified grammar rather than
#: caller-chosen keys.
#:
#: AI-337, measured 2026-08-31: `series` is in `_UNSAFE_KEYS` because a raw
#: ECharts option smuggled into the envelope would be spelled that way -- and
#: `bindings.series` is also a governed well of `visualization-spec.v1`, which
#: `visualization_specs.normalize_document` emits for EVERY document, filled or
#: empty. So the scan refused every real, normalized Visualization Spec, and the
#: render tool could not draw one. Nothing had noticed because the only spec that
#: had ever reached it was a hand-written literal with no `series` key.
#:
#: The exemption is the well NAME only, and only at this exact path: the value is
#: still walked, so an option object hidden under `bindings.series` is refused on
#: its own keys. The list is IMPORTED from the grammar, never retyped -- a well
#: added there must not need a second edit here to become renderable.
_GOVERNED_WELL_NODE_PATH_SUFFIX = ".render_input.spec.document.bindings"


class RenderAppPayloadRefused(ValueError):
    """A render payload cannot safely or honestly cross the MCP App seam."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def compose_render_input(
    *,
    result: dict[str, Any],
    spec: dict[str, Any],
    pins: dict[str, Any],
    profile: str,
    display: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the runtime's closed five-field input from named structural parts."""
    return {
        "result": deepcopy(result),
        "spec": deepcopy(spec),
        "pins": deepcopy(pins),
        "profile": profile,
        "display": deepcopy(display) if display is not None else {},
    }


def project_result_manifest(manifest: Any) -> dict[str, Any]:
    """Allowlist frozen Result metadata that a renderer may inspect."""
    if not isinstance(manifest, dict):
        return {}
    projected = {
        key: _project_safe_manifest_value(value)
        for key, value in manifest.items()
        if key in _SAFE_RESULT_MANIFEST_KEYS
    }
    if "time_window" not in projected and isinstance(manifest.get("time"), dict):
        projected["time_window"] = _project_safe_manifest_value(manifest["time"])
    if "unavailable_reason" in projected:
        reason = _safe_unavailable_reason(projected["unavailable_reason"])
        if reason is None:
            projected.pop("unavailable_reason", None)
        else:
            projected["unavailable_reason"] = reason
    if "missing_link" in projected:
        missing_link = _safe_missing_link(projected["missing_link"])
        if missing_link is None:
            projected.pop("missing_link", None)
        else:
            projected["missing_link"] = missing_link
    provenance = manifest.get("provenance")
    if isinstance(provenance, dict):
        safe_provenance = {
            key: deepcopy(value)
            for key, value in provenance.items()
            if key in _SAFE_PROVENANCE_KEYS and isinstance(value, str)
        }
        values = provenance.get("values")
        if isinstance(values, list):
            safe_values = [
                {
                    key: deepcopy(value)
                    for key, value in entry.items()
                    if key in _SAFE_VALUE_PROVENANCE_KEYS and isinstance(value, str)
                }
                for entry in values
                if isinstance(entry, dict)
            ]
            safe_provenance["values"] = [entry for entry in safe_values if entry]
        if safe_provenance:
            projected["provenance"] = safe_provenance
    datum_evidence = manifest.get("datum_evidence")
    if isinstance(datum_evidence, dict):
        projected["datum_evidence"] = {
            str(datum_key): {
                key: deepcopy(value)
                for key, value in entry.items()
                if key in _SAFE_VALUE_PROVENANCE_KEYS and isinstance(value, str)
            }
            for datum_key, entry in datum_evidence.items()
            if isinstance(entry, dict)
        }
    return projected


def _unsafe_path(value: Any, path: str = "$") -> str | None:
    if path == "$.toorow.feedback":
        # `token` is forbidden everywhere else.  This one exact sidecar is an
        # authenticated capability, so admit it only after its closed shape and
        # HMAC have been verified; malformed lookalikes remain unsafe.
        try:
            from core.analyze_feedback import verify_feedback_context  # noqa: PLC0415

            verify_feedback_context(value)
        except Exception:  # noqa: BLE001 -- validator converges to one safe refusal
            return f"{path}.token"
        return None
    if path.endswith(_BUSINESS_ROW_COLLECTION_PATH_SUFFIXES) and isinstance(value, list):
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                found = _unsafe_path(row, f"{path}[{index}]")
                if found:
                    return found
                continue
            # Result column names are business data, even when they resemble a
            # control key. Their cell values are still recursively inspected.
            for raw_key, child in row.items():
                found = _unsafe_path(child, f"{path}[{index}].{raw_key}")
                if found:
                    return found
        return None
    if path.endswith(_GOVERNED_WELL_NODE_PATH_SUFFIX) and isinstance(value, dict):
        from core.visualization_families import WELL_ROLES  # noqa: PLC0415

        for raw_key, child in value.items():
            key = str(raw_key)
            if key not in WELL_ROLES and key.casefold() in _UNSAFE_KEYS:
                return f"{path}.{key}"
            found = _unsafe_path(child, f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.casefold() in _UNSAFE_KEYS:
                return f"{path}.{key}"
            found = _unsafe_path(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _unsafe_path(child, f"{path}[{index}]")
            if found:
                return found
    return None


def serialized_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def validate_render_app_payload(
    payload: Any, *, existing_meta: dict[str, Any] | None = None
) -> None:
    """Validate the closed v1 wrapper, exact pins, safe shape and aggregate budget."""
    errors = sorted(_validator().iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
        )
        raise RenderAppPayloadRefused(
            "invalid_render_app_payload",
            f"The render app payload is invalid at {path}: {error.message}",
            details={"path": path},
        )

    pins = payload["render_input"]["pins"]
    for name, raw_value in pins.items():
        value = str(raw_value).strip()
        if value.casefold() in _PLACEHOLDER_PINS or "unbuilt" in value.casefold():
            raise RenderAppPayloadRefused(
                "invalid_render_app_payload",
                f"The render pin {name} must identify an exact delivered build.",
                details={"path": f"$.render_input.pins.{name}"},
            )

    unsafe = _unsafe_path(payload)
    if not unsafe:
        unsafe = _unsafe_path(existing_meta or {})
    if unsafe:
        raise RenderAppPayloadRefused(
            "unsafe_render_payload",
            f"The render app payload contains forbidden control material at {unsafe}.",
            details={"path": unsafe},
        )

    aggregate = dict(existing_meta or {})
    aggregate[APP_PAYLOAD_META_KEY] = payload
    measured = serialized_bytes(aggregate)
    if measured > MAX_RENDER_TOOL_META_BYTES:
        raise RenderAppPayloadRefused(
            "render_payload_over_budget",
            f"The render app metadata is {measured} bytes; the limit is "
            f"{MAX_RENDER_TOOL_META_BYTES} bytes.",
            details={"measured": measured, "budget": MAX_RENDER_TOOL_META_BYTES},
        )


def compose_render_app_payload(
    *,
    render_input: dict[str, Any],
    moved: dict[str, Any] | None = None,
    existing_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project and validate the one typed render payload placed in ToolResult `_meta`."""
    payload: dict[str, Any] = {
        "schema_version": APP_PAYLOAD_SCHEMA_VERSION,
        "kind": APP_PAYLOAD_KIND,
        "render_input": deepcopy(render_input),
    }
    if moved:
        payload["moved"] = deepcopy(moved)
    validate_render_app_payload(payload, existing_meta=existing_meta)
    return payload


_RUNTIME_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "bundle_sha256",
        "runtime_build",
        "theme_version",
        "formatter_version",
        "renderers",
    }
)
_RENDERER_MANIFEST_KEYS = frozenset(
    {"renderer_build", "schema_versions", "profiles"}
)


def _exact_pin(value: Any) -> bool:
    text = value.strip() if isinstance(value, str) else ""
    folded = text.casefold()
    return bool(text) and folded not in _PLACEHOLDER_PINS and "unbuilt" not in folded


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_runtime_manifest(bundle_path: str | Path | None = None) -> dict[str, Any]:
    """Load the manifest beside the exact HTML bundle and verify its byte hash."""
    if bundle_path is None:
        from core.visualization_runtime_resource import runtime_bundle_path  # noqa: PLC0415

        bundle = runtime_bundle_path()
    else:
        bundle = Path(bundle_path)
    manifest_path = bundle.with_name("runtime-manifest.json")
    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        bundle_bytes = bundle.read_bytes()
    except (OSError, ValueError, TypeError) as exc:
        raise RenderAppPayloadRefused(
            "runtime_manifest_unavailable",
            "The deployed visualization runtime manifest is unavailable. Rebuild the MCP App.",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != _RUNTIME_MANIFEST_KEYS:
        raise RenderAppPayloadRefused(
            "runtime_manifest_invalid",
            "The deployed visualization runtime manifest has an unsupported shape. Rebuild it.",
        )
    if raw.get("schema_version") != 1 or not isinstance(raw.get("renderers"), dict):
        raise RenderAppPayloadRefused(
            "runtime_manifest_invalid",
            "The deployed visualization runtime manifest version is unsupported. Rebuild it.",
        )
    actual = hashlib.sha256(bundle_bytes).hexdigest()
    expected = raw.get("bundle_sha256")
    if not isinstance(expected, str) or not hmac.compare_digest(actual, expected):
        raise RenderAppPayloadRefused(
            "runtime_manifest_mismatch",
            "The visualization runtime manifest does not match the served HTML. Rebuild both.",
            details={"actual_bundle_sha256": actual},
        )
    for field in ("runtime_build", "theme_version", "formatter_version"):
        if not _exact_pin(raw.get(field)):
            raise RenderAppPayloadRefused(
                "runtime_manifest_invalid",
                f"The visualization runtime manifest has no exact {field}.",
            )
    for family, declaration in raw["renderers"].items():
        if not isinstance(family, str) or not isinstance(declaration, dict):
            raise RenderAppPayloadRefused(
                "runtime_manifest_invalid", "A renderer manifest entry is malformed."
            )
        if set(declaration) != _RENDERER_MANIFEST_KEYS or not _exact_pin(
            declaration.get("renderer_build")
        ):
            raise RenderAppPayloadRefused(
                "runtime_manifest_invalid",
                f'The renderer manifest entry for "{family}" is incomplete.',
            )
        versions = declaration.get("schema_versions")
        profiles = declaration.get("profiles")
        if (
            not isinstance(versions, dict)
            or set(versions) != {"min", "max"}
            or not all(isinstance(versions[key], int) for key in ("min", "max"))
            or not isinstance(profiles, list)
            or not profiles
            or not all(isinstance(profile, str) for profile in profiles)
        ):
            raise RenderAppPayloadRefused(
                "runtime_manifest_invalid",
                f'The renderer capabilities for "{family}" are malformed.',
            )
    return raw


def resolve_runtime_pins(
    manifest: dict[str, Any], *, family: str, schema_version: int, profile: str
) -> dict[str, str]:
    """Resolve exact pins from the delivered build, never from a historical `latest`."""
    declaration = manifest.get("renderers", {}).get(family)
    if not isinstance(declaration, dict):
        raise RenderAppPayloadRefused(
            "runtime_build_unavailable",
            f'This runtime does not include a renderer for "{family}". Choose a compatible visual.',
        )
    versions = declaration["schema_versions"]
    if not versions["min"] <= schema_version <= versions["max"] or profile not in declaration[
        "profiles"
    ]:
        raise RenderAppPayloadRefused(
            "runtime_build_unavailable",
            "The deployed renderer does not support this Spec version and responsive profile.",
        )
    return {
        "runtime_build": manifest["runtime_build"],
        "renderer_build": declaration["renderer_build"],
        "theme_version": manifest["theme_version"],
        "formatter_version": manifest["formatter_version"],
    }
