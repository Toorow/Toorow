"""toorow -- Adaptation author-and-self-test MCP surface (Story 22.22, AD-3).

Exposes ONE tool through ``register(mcp)`` so the MCP-host LLM (on the user's
behalf) can submit a ``.py`` adaptation + a bounded sample and get the mapping
result back to iterate BEFORE proposing it:

  * ``file_source_test_adaptation`` -- Operations, READ. Ships the ``.py`` + the
    sample to the ISOLATED worker (``core.adaptation_executor``) and returns
    {rows, rejected, logs, resource_usage}. It holds NO DB write capability of its
    own and leaks NO credential/connection/DB handle to the worker (the executor
    ships only code + bytes + template).

Mirrors the metric_semantics_mcp / mapping_proposal_mcp shape: local
``_envelope`` / ``_tool_error`` / ``_identity`` / ``_result`` helpers, lazy
``core.*`` imports, ASCII-only, registered via ``mcp_profiles.register_profiled``
so the tool carries its capability declaration and passes ``validate_catalog``.
"""

from __future__ import annotations

import base64
import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"
# A bounded sample cap: the host LLM iterates on a SMALL slice, never a full file.
_MAX_SAMPLE_BYTES = 2 * 1024 * 1024


def _envelope(data: dict) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415

    return ToolResult(content=summary, structured_content=_envelope(data))


def register(mcp) -> None:
    """Register the adaptation self-test tool on *mcp* (one hook, called from main)."""
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    def file_source_test_adaptation(
        py_source: str,
        sample_base64: str,
        template: dict | None = None,
    ):
        """Run a .py adaptation on a bounded sample in the ISOLATED worker; return the mapping.

        The host LLM authors ``py_source`` (which must define
        ``adapt(input_bytes, template) -> {"rows": [...], "rejected": [...]}``) and
        passes a base64 sample. The tool ships ONLY the code + bytes + template to the
        out-of-process sandbox worker (never the credentialed server; AD-3) and returns
        {rows (bounded), rejected, logs, resource_usage}. It performs NO DB write and
        hands NO credential to the worker. A bounded worker error (forbidden import,
        adaptation exception, wall-time, output too large) is returned as ok=false with
        a stable error code -- never a leaked traceback.
        """
        from core.adaptation_executor import (  # noqa: PLC0415
            AdaptationExecutionError,
            execute_adaptation_preview,
        )

        identity = _identity()
        if not isinstance(py_source, str) or not py_source.strip():
            raise _tool_error("missing_param", "py_source is required.")
        try:
            sample = base64.b64decode(sample_base64 or "", validate=True)
        except (ValueError, TypeError) as exc:
            raise _tool_error("bad_sample", "sample_base64 is not valid base64.") from exc
        if len(sample) > _MAX_SAMPLE_BYTES:
            raise _tool_error("sample_too_large", "the sample exceeds the bounded cap.")

        try:
            preview = execute_adaptation_preview(py_source, sample, template or {})
        except AdaptationExecutionError as exc:
            raise _tool_error(exc.code, exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("adaptation self-test failed for %s: %s", identity, exc)
            raise _tool_error("server_error", "adaptation self-test failed.") from exc

        if not preview.get("ok"):
            err = preview.get("error") or {}
            summary = f"Adaptation self-test failed: {err.get('code', 'error')}."
            return _result(summary, {"ok": False, **preview})

        n = preview.get("row_count", 0)
        summary = (
            f"Adaptation produced {n} canonical row(s), "
            f"{preview.get('rejected_count', 0)} rejected."
        )
        return _result(summary, {"ok": True, **preview})

    register_profiled(
        mcp,
        file_source_test_adaptation,
        profile="operations",
        effect="read",  # sandbox self-test only; no DB write, no publication
        data_class="operational",
        confirmation_mode="none",
        name="file_source_test_adaptation",
    )
