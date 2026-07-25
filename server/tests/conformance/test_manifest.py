"""Layer 1: Manifest schema validation — Story 1.8 (T2.1–T2.4).

Validates ``manifest.json`` from ``module_path`` against the canonical
``server/core/schemas/manifest.schema.json`` (established in Story 1.3).

Failures are collected (not fail-fast per-error), then reported together.
If any error is found the session-level ``_layer1_status`` flag is flipped
so subsequent layers skip gracefully (AC2 fail-fast).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

pytestmark = pytest.mark.conformance_layer_1

# Path to the canonical schema — resolved relative to *this* file so the test
# works regardless of the cwd at invocation time.
_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent  # server/
    / "core"
    / "schemas"
    / "manifest.schema.json"
)
_CAPABILITY_SCHEMA_PATH = _SCHEMA_PATH.with_name("source-capabilities.schema.json")


def _load_schema() -> dict:
    if not _SCHEMA_PATH.exists():
        pytest.fail(f"[manifest] manifest.schema.json not found at {_SCHEMA_PATH}")
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _format_error(err: jsonschema.ValidationError) -> str:
    path = "$." + ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "$"
    return f"[manifest] {path}: {err.message}"


# ---------------------------------------------------------------------------
# Layer 1 test
# ---------------------------------------------------------------------------


def test_manifest_schema(manifest: dict, _layer1_status: list[bool]) -> None:
    """Validate manifest.json against manifest.schema.json (AC2).

    Collects all errors before reporting so every broken field is surfaced
    in a single failure message (T2.2).
    """
    schema = _load_schema()
    capability_schema = json.loads(
        _CAPABILITY_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    registry = Registry().with_resource(
        capability_schema["$id"], Resource.from_contents(capability_schema)
    )
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    errors = list(validator.iter_errors(manifest))

    if errors:
        # Mark layer 1 as failed so subsequent layers skip
        _layer1_status[0] = False
        formatted = "\n".join(_format_error(e) for e in errors)
        pytest.fail(
            f"[manifest] schema validation failed with {len(errors)} error(s):\n{formatted}"
        )


# ---------------------------------------------------------------------------
# AI-30 (Story 5.1): module_kind='context' vs actual behaviour
# ---------------------------------------------------------------------------

# fact_daily_kpi write shapes we flag: INSERT/UPDATE/MERGE/COPY targeting the table,
# or a dbt/warehouse write. A bare mention in a comment/docstring is NOT a write —
# we look for the table name adjacent to a write verb (case-insensitive).
_FACT_WRITE_RE = __import__("re").compile(
    r"\b(insert\s+into|update|merge\s+into|copy)\b[^;]{0,120}?fact_daily_kpi"
    r"|fact_daily_kpi[^;]{0,120}?\b(values|set)\b",
    __import__("re").IGNORECASE,
)


def test_context_module_no_fact_daily_kpi_write(
    module_path, manifest: dict, _layer1_status: list[bool]
) -> None:
    """AI-30: a module declaring ``module_kind='context'`` must NOT write fact_daily_kpi.

    Conformance validates the manifest's declared ``module_kind`` against the module's
    actual behaviour: a context module writes ``app.context_events`` (Postgres), never
    ``fact_daily_kpi`` (the KPI mart grain). We grep ``connector.py`` for a
    fact_daily_kpi *write* (INSERT/UPDATE/MERGE/COPY ... fact_daily_kpi). Mentions in
    comments/docstrings that are not adjacent to a write verb do not trip the check, so
    the HG-2 self-documentation ("this module does NOT write fact_daily_kpi") is safe.

    Epic 31.2: gated by per-profile landing, not module_kind. The check applies
    only to a PURELY-context connector (EVERY profile lands in context_events).
    A MIXED connector (youtube: kpi profiles + a context_events event profile)
    LEGITIMATELY writes fact_daily_kpi for its kpi profiles, so this check does
    not apply to it -- its kpi profiles are validated by Layers 2/4 and its event
    profile by Layer 5.
    """
    from core.context_events import resolve_landing  # noqa: PLC0415

    module_kind = manifest.get("module_kind", "kpi")
    profiles = manifest.get("report_profiles", [])
    all_context = bool(profiles) and all(
        resolve_landing(p, module_kind) == "context_events" for p in profiles
    )
    # Fallback for a manifest with no report_profiles: honour the legacy hint.
    if not profiles:
        all_context = module_kind == "context"
    if not all_context:
        pytest.skip(
            "not a pure-context connector (some/all profiles land fact_daily_kpi) "
            "-- AI-30 check not applicable"
        )

    connector_py = module_path / "connector.py"
    if not connector_py.exists():
        pytest.fail(f"[manifest] connector.py not found at {connector_py}")

    source = connector_py.read_text(encoding="utf-8")
    match = _FACT_WRITE_RE.search(source)
    if match:
        _layer1_status[0] = False
        pytest.fail(
            "[manifest] AI-30 conformance violation: module declares "
            "module_kind='context' but connector.py appears to WRITE fact_daily_kpi "
            f"(matched: {match.group(0)[:80]!r}). Context modules must write "
            "app.context_events only, never the KPI mart."
        )
