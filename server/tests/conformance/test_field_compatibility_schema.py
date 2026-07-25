"""Conformance -- field_compatibility versioned schema gate (Story 26.1, B).

Two guarantees:
  1. The versioned contract file (core/schemas/field-compatibility.schema.json,
     the runtime home of every platform schema -- manifest, api-catalog,
     source-capabilities) exists, is a valid Draft 2020-12 schema, and pins
     schema_version "1". A version bump is a deliberate, reviewed act.
  2. Every module that OPTS IN to the core contract (a ``field_compatibility``
     block carrying the ``schema_version`` marker in its
     catalog_sources/catalog_sources.json) validates against the contract.
     Modules without the block -- or with a MODULE-LOCAL companion block that
     has no ``schema_version`` (a format consumed by the module's own code,
     ignored by the core engine) -- are untouched (backward-compatible no-op).

HG-1 (AD-2): no module-specific strings here -- modules are discovered
dynamically. No shared counting assertions (coordination constraint 26.1).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

_SERVER_DIR = Path(__file__).parents[2]  # server/tests/conformance -> server
_SCHEMA_PATH = _SERVER_DIR / "core" / "schemas" / "field-compatibility.schema.json"
_MODULES_DIR = _SERVER_DIR / "modules"

_SCHEMA_ID = "https://toorow.dev/schemas/field-compatibility.schema.json"


def _module_dirs() -> list[Path]:
    if not _MODULES_DIR.is_dir():
        return []
    return sorted(entry for entry in _MODULES_DIR.iterdir() if entry.is_dir())


def _declared_blocks() -> list[tuple[str, dict]]:
    """(module_name, block) for every module OPTING IN to the core contract.

    Mirrors core.field_compat.load_module_rules: a block without the
    ``schema_version`` marker is a module-local companion format the core
    engine ignores (no-op), so it is not held to the versioned contract.
    """
    blocks: list[tuple[str, dict]] = []
    for module_dir in _module_dirs():
        sources_path = module_dir / "catalog_sources" / "catalog_sources.json"
        if not sources_path.is_file():
            continue
        try:
            sources = json.loads(sources_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue  # the module's own gates cover unreadable sources files
        if not isinstance(sources, dict):
            continue
        block = sources.get("field_compatibility")
        if isinstance(block, dict) and "schema_version" in block:
            blocks.append((module_dir.name, block))
    return blocks


# ---------------------------------------------------------------------------
# 1. The versioned contract file
# ---------------------------------------------------------------------------


def test_schema_file_exists() -> None:
    assert _SCHEMA_PATH.is_file(), (
        f"field-compatibility.schema.json missing at {_SCHEMA_PATH}"
    )


def test_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_schema_identity_and_version_are_pinned() -> None:
    """$id and the pinned schema_version const are the versioning contract:
    changing either is a deliberate, reviewed platform-contract evolution."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$id"] == _SCHEMA_ID
    assert schema["properties"]["schema_version"] == {"const": "1"}


def test_schema_declares_the_five_rule_kinds() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    kinds = set(schema["$defs"]["rule"]["properties"]["kind"]["enum"])
    assert kinds == {
        "mutually_exclusive",
        "requires",
        "selectable_set",
        "only_alone",
        "max_selection",
    }


# ---------------------------------------------------------------------------
# 2. Every declaring module validates against the contract
# ---------------------------------------------------------------------------


def test_every_declared_block_validates() -> None:
    from core.field_compat import validate_rules

    failures: list[str] = []
    for module_name, block in _declared_blocks():
        errors = validate_rules(block)
        if errors:
            failures.append(f"[{module_name}] " + "; ".join(errors))
    assert not failures, (
        "field_compatibility blocks failing the versioned contract:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_declared_blocks_load_through_the_core_loader_hook() -> None:
    """load_module_rules (the loader's validation hook) accepts every module."""
    from core.field_compat import FieldCompatibilityError, load_module_rules

    failures: list[str] = []
    for module_dir in _module_dirs():
        try:
            load_module_rules(module_dir)
        except FieldCompatibilityError as exc:
            failures.append(f"[{module_dir.name}] {exc}")
    assert not failures, (
        "modules whose field_compatibility rules would be refused at load time:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_declaring_modules_smoke() -> None:
    """Blocks, when declared, are non-empty rule sets (a declared-but-empty
    contract is a mistake, not a compatibility statement)."""
    for module_name, block in _declared_blocks():
        if not isinstance(block, dict):
            pytest.fail(f"[{module_name}] field_compatibility must be an object")
        assert block.get("rules"), (
            f"[{module_name}] field_compatibility.rules must not be empty"
        )
