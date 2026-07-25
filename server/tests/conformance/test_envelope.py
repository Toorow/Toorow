"""Layer 2: Envelope shape validation — Story 1.8 (T3.1–T3.6).

For each report profile declared in ``manifest.json``, this layer:
  1. Imports ``connector.py`` via ``importlib`` (same strategy as Story 1.3).
  2. Resolves the MCP tool whose name matches the profile id (or falls back
     to the single exported tool when there is only one).
  3. Calls the tool with stub parameters.
  4. Validates the return value against the canonical AD-1 envelope schema.
  5. Checks that the tool's text summary (docstring used as text channel) is
     ≤30 lines (AD-1 token-burn split).

Skips if layer 1 failed (_layer1_status[0] is False).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest

pytestmark = pytest.mark.conformance_layer_2

_ENVELOPE_SCHEMA_PATH = Path(__file__).parent / "schemas" / "envelope.schema.json"


def _load_envelope_schema() -> dict:
    return json.loads(_ENVELOPE_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_connector(module_path: Path) -> ModuleType:
    """Import connector.py from module_path via importlib (T3.2)."""
    connector_file = module_path / "connector.py"
    if not connector_file.exists():
        pytest.fail(f"[envelope] connector.py not found at {connector_file}")

    module_name = f"connector_{module_path.name}"
    # Remove any stale cached module to allow re-import across parametrized runs
    sys.modules.pop(module_name, None)

    try:
        spec = importlib.util.spec_from_file_location(module_name, connector_file)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        pytest.fail(f"[envelope] connector.py could not be loaded: {exc}")

    return mod


def _get_mcp_tools(connector_mod: ModuleType) -> dict:
    """Return a dict of tool_name -> tool_fn from the module's mcp_app.

    FastMCP 3.4.4 exposes the registry through the async public API only:
    ``await app.list_tools()`` then ``await app.get_tool(name)``; the returned
    tool object carries the underlying callable on ``.fn``.
    """
    import asyncio

    mcp_app = getattr(connector_mod, "mcp_app", None)
    if mcp_app is None:
        return {}

    async def _collect() -> dict:
        tools = {}
        for t in await mcp_app.list_tools():
            tool_obj = await mcp_app.get_tool(t.name)
            tools[t.name] = getattr(tool_obj, "fn", tool_obj)
        return tools

    try:
        return asyncio.run(_collect())
    except Exception:
        return {}


def _resolve_tool(connector_mod: ModuleType, profile_id: str) -> object:
    """Return the callable tool function for a given report profile id.

    Strategy (in order):
      1. Direct attribute lookup on the module: ``connector_mod.<profile_id>``
      2. mcp_app tools registry lookup by exact tool name == profile_id
      3. If mcp_app has exactly one tool, use it (single-tool modules)
      4. Return None — caller will emit a conformance failure.
    """
    # 1. Direct attribute
    fn = getattr(connector_mod, profile_id, None)
    if callable(fn):
        return fn

    # 2 + 3. mcp_app tools registry
    tools = _get_mcp_tools(connector_mod)
    if profile_id in tools:
        return tools[profile_id]
    # Single-tool fallback — covers single-tool modules where the profile id
    # does not match the Python function name (e.g. profile "standard_daily"
    # but function "get_ga4_report").
    if len(tools) == 1:
        return next(iter(tools.values()))

    return None


def _call_tool(fn: object, profile_id: str) -> dict:
    """Call the tool with stub conformance parameters.

    # AD-14: stub identity for conformance testing
    """
    stub_kwargs: dict = {
        "project_id": "conformance-test",  # AD-14: stub identity for conformance testing
    }
    sig = inspect.signature(fn)  # type: ignore[arg-type]
    # Only pass kwargs the function actually accepts
    accepted = {k: v for k, v in stub_kwargs.items() if k in sig.parameters}
    try:
        result = fn(**accepted)  # type: ignore[call-arg, operator]
    except Exception as exc:
        pytest.fail(f"[envelope] tool={profile_id}: tool raised an exception: {exc}")

    # review-1-8 F-01: data tools SHOULD return the dual-channel ToolResult
    # (AD-1 / server/core/README.md). Normalize: validate structured_content.
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    if not isinstance(result, dict):
        pytest.fail(
            f"[envelope] tool={profile_id}: returned {type(result).__name__}, "
            "expected dict envelope or ToolResult with structured_content"
        )
    return result


def _format_env_error(err: jsonschema.ValidationError, tool_name: str) -> str:
    path = "$." + ".".join(str(p) for p in err.absolute_path) if err.absolute_path else "$"
    return f"[envelope] tool={tool_name} path={path}: {err.message}"


# ---------------------------------------------------------------------------
# Layer 2 test
# ---------------------------------------------------------------------------


def test_envelope_shape(manifest: dict, module_path: Path, _layer1_status: list[bool]) -> None:
    """Validate every report profile tool returns the canonical AD-1 envelope (AC3).

    Skips for context modules (module_kind='context'): they produce context_events
    rows, not AD-1 envelope responses.  Layer 5 (test_context_events.py) validates
    context modules instead (Story 4.5, AC5).

    HG-8: the skip condition uses manifest.get('module_kind') generically —
    no module-specific name appears here.
    """
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping envelope layer")

    from core.context_events import resolve_landing  # noqa: PLC0415

    module_kind = manifest.get("module_kind", "kpi")
    all_profiles = manifest.get("report_profiles", [])
    if not all_profiles:
        pytest.skip("[envelope] no report_profiles declared in manifest")

    # Epic 31.2: per-PROFILE routing by landing. Only profiles landing in
    # fact_daily_kpi expose a data-returning MCP tool that must validate the
    # AD-1 envelope; a context_events-landing profile (an event profile) has no
    # report tool (it persists context_events via its pull) and is validated by
    # Layer 5 instead. A MIXED connector (youtube) validates only its kpi
    # profiles here; a pure context connector (github) has none -> skips.
    profiles = [
        p for p in all_profiles
        if resolve_landing(p, module_kind) == "fact_daily_kpi"
    ]
    if not profiles:
        pytest.skip(
            "[envelope] no fact_daily_kpi-landing profile — Layer 2 (envelope) is N/A; "
            "see Layer 5 (test_context_events.py) for context_events-landing profiles"
        )

    schema = _load_envelope_schema()
    validator = jsonschema.Draft202012Validator(schema)
    connector_mod = _load_connector(module_path)

    failures: list[str] = []

    for profile in profiles:
        profile_id: str = profile["id"]
        fn = _resolve_tool(connector_mod, profile_id)

        if fn is None:
            failures.append(
                f"[envelope] tool={profile_id}: no matching tool found in connector.py"
            )
            continue

        result = _call_tool(fn, profile_id)

        # --- Validate envelope shape ---
        env_errors = list(validator.iter_errors(result))
        for err in env_errors:
            failures.append(_format_env_error(err, profile_id))

        # --- Validate summary line count (≤30 lines, AD-1) ---
        doc = getattr(fn, "__doc__", "") or ""
        lines = [ln for ln in doc.splitlines() if ln.strip()]
        if len(lines) > 30:
            failures.append(
                f"[envelope] tool={profile_id}: summary exceeds 30 lines ({len(lines)} lines)"
            )

    if failures:
        pytest.fail("\n".join(failures))
