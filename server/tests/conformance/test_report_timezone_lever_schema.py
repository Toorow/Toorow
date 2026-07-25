"""Schema-validation tests for Story 39.8 -- the additive `report_timezone_lever` $def.

Validates that:
  - the updated source-capabilities schema is itself a valid Draft-2020-12 schema;
  - a `report_timezone_lever` object with a valid `locus` validates against the $def;
  - `additionalProperties:false` and the `locus` required/minLength constraints bite;
  - GAM's real manifest (which declares NO lever -- its timezone is a fixed network property)
    validates unchanged against the full manifest schema (has_lever:false, the common case);
  - a descriptor WITH a `report_timezone_lever` declared validates (lever present).

Offline: pure JSON + jsonschema, mirrors test_time_context_schema.py.
"""

from __future__ import annotations

import copy
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
    schema = _load(_MANIFEST_SCHEMA)
    capability_schema = _load(_SRC_CAPS_SCHEMA)
    registry = Registry().with_resource(
        capability_schema["$id"], Resource.from_contents(capability_schema)
    )
    return jsonschema.Draft202012Validator(schema, registry=registry)


def _lever_validator() -> jsonschema.Draft202012Validator:
    schema = _load(_SRC_CAPS_SCHEMA)
    lever_schema = dict(schema["$defs"]["report_timezone_lever"])
    lever_schema["$defs"] = schema["$defs"]
    return jsonschema.Draft202012Validator(lever_schema)


# ---------------------------------------------------------------------------
# Schema well-formedness + the $def exists
# ---------------------------------------------------------------------------


def test_source_capabilities_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_load(_SRC_CAPS_SCHEMA))


def test_report_timezone_lever_def_present_on_descriptor():
    schema = _load(_SRC_CAPS_SCHEMA)
    assert "report_timezone_lever" in schema["$defs"]
    assert "report_timezone_lever" in schema["$defs"]["descriptor"]["properties"]
    assert "report_timezone_lever" in schema["$defs"]["public_response"]["properties"]


# ---------------------------------------------------------------------------
# lever object validation
# ---------------------------------------------------------------------------


def test_lever_valid_with_locus():
    v = _lever_validator()
    assert list(v.iter_errors({"locus": "report_settings"})) == []
    assert list(v.iter_errors(None)) == []  # nullable => no lever


def test_lever_requires_locus_and_rejects_extras():
    v = _lever_validator()
    assert list(v.iter_errors({}))  # locus required
    assert list(v.iter_errors({"locus": ""}))  # minLength 1
    assert list(v.iter_errors({"locus": "report_settings", "surprise": 1}))  # no extras


# ---------------------------------------------------------------------------
# GAM (no lever) validates unchanged; a descriptor WITH a lever validates
# ---------------------------------------------------------------------------


def test_gam_manifest_has_no_lever_and_validates():  # 29 (GAM = fixed, no lever)
    validator = _manifest_validator()
    manifest = _load(_MODULES / "google-ad-manager" / "manifest.json")
    # GAM declares no lever (its timezone is a fixed NETWORK property, not a report filter).
    assert manifest["source_capabilities"].get("report_timezone_lever") is None
    errors = list(validator.iter_errors(manifest))
    assert errors == [], f"GAM manifest invalid: {[e.message for e in errors]}"


def test_manifest_with_declared_lever_validates():  # 29 (lever declared)
    validator = _manifest_validator()
    manifest = copy.deepcopy(_load(_MODULES / "google-ad-manager" / "manifest.json"))
    manifest["source_capabilities"]["report_timezone_lever"] = {"locus": "report_settings"}
    errors = list(validator.iter_errors(manifest))
    assert errors == [], f"manifest with lever invalid: {[e.message for e in errors]}"
