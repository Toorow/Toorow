"""toorow -- Adaptation executor: ship .py to the isolated worker (Story 22.21/22.22).

The server-side client that runs an LLM-authored ``.py`` adaptation by spawning the
dedicated out-of-process sandbox worker (``infra/sandbox/worker.py``) as a SEPARATE
process and communicating over stdin/stdout. The credentialed server NEVER
``exec``/``eval``s adaptation code in-process (AD-3), and ships ONLY
``{py_source, input_bytes, template}`` -- never a credential, connection, or DB
handle -- to the worker.

The parent-side subprocess timeout is the AUTHORITATIVE wall-time guard (it works on
every OS, including where the worker cannot self-install a SIGALRM). A timeout kills
the worker and returns a bounded ``wall_time_exceeded`` error.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# infra/sandbox/worker.py relative to the repo root (server/core/ -> repo root).
_WORKER = Path(__file__).resolve().parents[2] / "infra" / "sandbox" / "worker.py"

DEFAULT_WALL_SECONDS = 10
# A hard cap on the request we hand the worker (defense against an oversized ship).
MAX_PY_SOURCE_BYTES = 512 * 1024
MAX_INPUT_BYTES = 32 * 1024 * 1024


class AdaptationExecutionError(Exception):
    """A bounded executor failure (carries a stable code)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def run_adaptation(
    py_source: str,
    input_bytes: bytes,
    template: dict[str, Any],
    *,
    wall_seconds: int = DEFAULT_WALL_SECONDS,
    worker_path: Path | None = None,
) -> dict[str, Any]:
    """Run an adaptation in the isolated worker; return its bounded result dict.

    Returns ``{rows, rejected, logs, resource_usage}`` on success, or
    ``{"error": {code, message}}`` on a bounded worker/executor failure (forbidden
    import, adaptation exception, wall-time, output too large). Raises
    ``AdaptationExecutionError`` only for an executor-side precondition
    (oversized ship / missing worker), never for adaptation faults.
    """
    if not isinstance(py_source, str) or not py_source.strip():
        raise AdaptationExecutionError("empty_source", "no adaptation source to run.")
    if len(py_source.encode("utf-8")) > MAX_PY_SOURCE_BYTES:
        raise AdaptationExecutionError("source_too_large", "adaptation source exceeds the cap.")
    if len(input_bytes) > MAX_INPUT_BYTES:
        raise AdaptationExecutionError("input_too_large", "input bytes exceed the cap.")

    worker = Path(worker_path) if worker_path else _WORKER
    if not worker.exists():
        raise AdaptationExecutionError("worker_missing", "the sandbox worker is not deployed.")

    # ONLY the code, the bytes, and the template cross the boundary -- no credential,
    # no connection, no environment secret is handed to the worker.
    request = json.dumps({
        "py_source": py_source,
        "input_bytes_b64": base64.b64encode(input_bytes).decode("ascii"),
        "template": template or {},
        "wall_seconds": int(wall_seconds),
    })

    try:
        proc = subprocess.run(
            [sys.executable, str(worker)],
            input=request,
            capture_output=True,
            text=True,
            timeout=max(1, int(wall_seconds)) + 2,  # parent grace over the worker alarm
            check=False,
            # A minimal, credential-free environment (no inherited secrets).
            env={"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"error": {"code": "wall_time_exceeded",
                          "message": "the adaptation exceeded the wall-time limit."}}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"error": {"code": "worker_crash", "message": "the sandbox worker crashed."}}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": {"code": "bad_worker_output", "message": "unparseable worker output."}}


def execute_adaptation_preview(
    py_source: str,
    input_bytes: bytes,
    template: dict[str, Any],
    *,
    wall_seconds: int = DEFAULT_WALL_SECONDS,
    preview_limit: int = 100,
) -> dict[str, Any]:
    """Run an adaptation and return a BOUNDED preview (rows + rejects + logs).

    For the MCP author-and-self-test loop (Story 22.22): the host LLM iterates on the
    ``.py`` and sees the mapping result WITHOUT any DB write or publication. Rows are
    truncated to ``preview_limit`` so the preview stays bounded.
    """
    result = run_adaptation(py_source, input_bytes, template, wall_seconds=wall_seconds)
    if "error" in result:
        return {"ok": False, "error": result["error"], "rows": [], "rejected": [], "logs": []}
    rows = result.get("rows", [])
    return {
        "ok": True,
        "rows": rows[:preview_limit],
        "row_count": len(rows),
        "rejected": result.get("rejected", [])[:preview_limit],
        "rejected_count": len(result.get("rejected", [])),
        "logs": result.get("logs", []),
        "resource_usage": result.get("resource_usage", {}),
        "truncated": len(rows) > preview_limit,
    }
