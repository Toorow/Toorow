"""Schema-validation tests for Story 39.7 -- the additive `time_context` descriptor sub-object.

Validates that:
  - the updated source-capabilities schema is itself a valid Draft-2020-12 schema;
  - a `time_context` object with the reference postures (network/gap, property/gap,
    fixed+fixed_zone, assume+assumed_zone) validates against `$defs.time_context`;
  - the conditional requires bite (locus='fixed' without fixed_zone; fallback='assume'
    without assumed_zone => invalid);
  - GAM's + GA4's real manifests (which now carry `time_context`) validate against the full
    manifest schema (the descriptor $refs the sub-object);
  - a manifest WITHOUT `time_context` still validates (ADDITIVE / backward-compatible).

Offline: pure JSON + jsonschema, mirrors test_broken_module / test_field_compatibility_schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

_CORE_SCHEMAS = Path(__file__).parent.parent.parent / "core" / "schemas"
_SRC_CAPS_SCHEMA = _CORE_SCHEMAS / "source-capabilities.schema.json"
_MANIFEST_SCHEMA = _CORE_SCHEMAS / "manifest.schema.json"
_MODULES = Path(__file__).parent.parent.parent / "modules"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_validator() -> jsonschema.Draft202012Validator:
    """A manifest validator with the source-capabilities schema registered for $ref resolution
    (mirrors test_manifest.py's registry wiring)."""
    schema = _load(_MANIFEST_SCHEMA)
    capability_schema = _load(_SRC_CAPS_SCHEMA)
    registry = Registry().with_resource(
        capability_schema["$id"], Resource.from_contents(capability_schema)
    )
    return jsonschema.Draft202012Validator(schema, registry=registry)


def _time_context_validator() -> jsonschema.Draft202012Validator:
    """A validator bound to `$defs.time_context` of the source-capabilities schema."""
    schema = _load(_SRC_CAPS_SCHEMA)
    tc_schema = dict(schema["$defs"]["time_context"])
    # Attach $defs so any internal $ref resolves (defensive; time_context has none today).
    tc_schema["$defs"] = schema["$defs"]
    return jsonschema.Draft202012Validator(tc_schema)


# ---------------------------------------------------------------------------
# Schema well-formedness
# ---------------------------------------------------------------------------


def test_source_capabilities_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_load(_SRC_CAPS_SCHEMA))


# ---------------------------------------------------------------------------
# time_context sub-object validation
# ---------------------------------------------------------------------------


def test_time_context_reference_postures_valid():
    v = _time_context_validator()
    valid = [
        {"locus": "network", "fallback": "gap"},
        {"locus": "property", "fallback": "gap"},
        {"locus": "account", "fallback": "gap"},
        {"locus": "fixed", "fallback": "gap", "fixed_zone": "America/Los_Angeles"},
        {"locus": "none", "fallback": "gap"},
        {"locus": "network", "fallback": "assume", "assumed_zone": "Europe/Paris"},
    ]
    for obj in valid:
        assert list(v.iter_errors(obj)) == [], f"expected valid: {obj}"


def test_time_context_fixed_requires_fixed_zone():
    v = _time_context_validator()
    assert list(v.iter_errors({"locus": "fixed", "fallback": "gap"}))  # missing fixed_zone


def test_time_context_assume_requires_assumed_zone():
    v = _time_context_validator()
    assert list(v.iter_errors({"locus": "network", "fallback": "assume"}))  # missing assumed_zone


def test_time_context_rejects_unknown_locus_and_extra_keys():
    v = _time_context_validator()
    assert list(v.iter_errors({"locus": "bogus", "fallback": "gap"}))
    assert list(v.iter_errors({"locus": "network", "fallback": "gap", "surprise": 1}))
    assert list(v.iter_errors({"locus": "network"}))  # fallback required


# ---------------------------------------------------------------------------
# Real manifests validate; and a no-time_context descriptor still validates.
# ---------------------------------------------------------------------------


def test_reference_manifests_with_time_context_validate():
    validator = _manifest_validator()
    for module in ("google-ad-manager", "google-analytics"):
        manifest = _load(_MODULES / module / "manifest.json")
        tc = manifest["source_capabilities"].get("time_context")
        assert isinstance(tc, dict), f"{module}: expected a time_context declaration"
        errors = list(validator.iter_errors(manifest))
        assert errors == [], f"{module} manifest invalid: {[e.message for e in errors]}"


def test_manifest_without_time_context_still_validates():
    """ADDITIVE / backward-compatible: dropping time_context keeps the manifest valid."""
    validator = _manifest_validator()
    manifest = _load(_MODULES / "google-ad-manager" / "manifest.json")
    manifest["source_capabilities"].pop("time_context", None)
    errors = list(validator.iter_errors(manifest))
    assert errors == [], f"no-time_context manifest should validate: {[e.message for e in errors]}"
