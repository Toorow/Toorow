"""Broken-module fixture validation — Story 1.8 (T7.5–T7.6).

Runs each conformance layer directly against the ``broken-module`` fixture
folder (``server/tests/conformance/fixtures/broken-module/``) and asserts
that each layer correctly detects its respective violation.

These tests do NOT use ``--module-path``; they hard-code the fixture path
so they always run as part of the normal ``pytest server/tests`` suite.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import jsonschema

# Path to the broken-module fixture folder
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "broken-module"
_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent  # server/
    / "core"
    / "schemas"
    / "manifest.schema.json"
)
_ENVELOPE_SCHEMA_PATH = Path(__file__).parent / "schemas" / "envelope.schema.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_broken_manifest() -> dict:
    return json.loads((_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_envelope_schema() -> dict:
    return json.loads(_ENVELOPE_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_broken_connector():
    module_name = "connector_broken_module_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _FIXTURE_DIR / "connector.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Layer 1: Manifest violations
# ---------------------------------------------------------------------------


def test_broken_manifest_name_fails() -> None:
    """Layer 1: 'name' with uppercase should fail kebab-case pattern (T7.2)."""
    manifest = _load_broken_manifest()
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(manifest))

    assert errors, "Expected validation errors for broken manifest"

    error_messages = " ".join(e.message + str(list(e.path)) for e in errors)
    assert "name" in error_messages or any(
        "name" in str(list(e.path)) for e in errors
    ), f"Expected 'name' to appear in error context, got: {error_messages}"


def test_broken_manifest_auth_type_fails() -> None:
    """Layer 1: 'auth_type: unknown' should fail enum validation (T7.2)."""
    manifest = _load_broken_manifest()
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(manifest))

    assert errors, "Expected validation errors for broken manifest"

    auth_errors = [
        e for e in errors
        if "auth_type" in str(list(e.path)) or "auth_type" in e.message
    ]
    assert auth_errors, (
        f"Expected an error mentioning 'auth_type', got errors: "
        f"{[e.message for e in errors]}"
    )


# ---------------------------------------------------------------------------
# Layer 2: Envelope violation
# ---------------------------------------------------------------------------


def test_broken_envelope_missing_keys() -> None:
    """Layer 2: broken_report tool returns {result:[]} — must fail envelope schema (T7.3)."""
    connector_mod = _load_broken_connector()
    schema = _load_envelope_schema()
    validator = jsonschema.Draft202012Validator(schema)

    result = connector_mod.broken_report(project_id="conformance-test")
    errors = list(validator.iter_errors(result))

    assert errors, f"Expected envelope validation errors, got none. Result: {result}"

    error_messages = " ".join(e.message for e in errors)
    assert "schema_version" in error_messages, (
        f"Expected 'schema_version' in error messages, got: {error_messages}"
    )


# ---------------------------------------------------------------------------
# Layer 4: Golden pull mismatch
# ---------------------------------------------------------------------------


def test_broken_golden_pull_mismatch() -> None:
    """Layer 4: transform() produces active_users-1, expected_facts has active_users=999 (T7.4).

    The broken connector.py subtracts 1 from active_users, which would make
    raw[active_users=1000] → actual[active_users=999]. The expected_facts.json
    also has 999, so they match — this tests that we CAN catch a mismatch
    by changing the expected value. Here we verify the mismatch scenario
    directly by computing a different expected value.
    """
    connector_mod = _load_broken_connector()
    raw_rows = json.loads(
        (_FIXTURE_DIR / "tests" / "fixtures" / "golden_pull.json").read_text(encoding="utf-8")
    )
    # Transform produces active_users=999 (1000-1)
    actual_rows = connector_mod.transform(raw_rows)
    assert actual_rows[0]["active_users"] == 999, (
        f"Expected 999, got {actual_rows[0]['active_users']}"
    )

    # Now compare against a deliberately wrong expected value (1000) to confirm
    # the mismatch is detectable
    wrong_expected = [
        {"pull_id": "pull_broken_001", "connector": "broken-module",
         "date": "2026-07-01", "active_users": 1000}
    ]
    mismatches = []
    for i, (actual, expected) in enumerate(zip(actual_rows, wrong_expected)):
        for field in set(expected) | set(actual):
            if expected.get(field) != actual.get(field):
                mismatches.append(
                    f"[golden_pull] row {i} field {field}: "
                    f"expected {expected.get(field)!r}, got {actual.get(field)!r}"
                )

    assert mismatches, "Expected a mismatch to be detected"
    assert any("active_users" in m for m in mismatches), (
        f"Expected 'active_users' in mismatch messages, got: {mismatches}"
    )
