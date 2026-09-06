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
import os
import shlex
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

_HARD_SANDBOX_ENV = "TOOROW_ADAPTATION_SANDBOX_COMMAND"
_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
_REQUIRED_ISOLATION = frozenset(
    {
        "network_none",
        "host_filesystem_none",
        "rootfs_read_only",
        "memory_limit",
        "cpu_limit",
        "wall_time_limit",
        "process_limit",
        "seccomp",
        "non_root",
    }
)


class AdaptationExecutionError(Exception):
    """A bounded executor failure (carries a stable code)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _production_requires_hard_sandbox() -> bool:
    environment = (
        os.environ.get("TOOROW_ENVIRONMENT")
        or os.environ.get("TOOROW_ENV")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("APP_ENV")
    )
    if environment is not None:
        normalized = environment.strip().lower()
        # A declared-but-empty environment is still undeclared. Fail closed just
        # like the completely absent case outside pytest.
        return normalized in _PRODUCTION_ENVIRONMENTS or (
            not normalized and "PYTEST_CURRENT_TEST" not in os.environ
        )
    # Pytest exercises the portable defense-in-depth worker deliberately. Every
    # non-test runtime with no declared environment fails closed as production.
    return "PYTEST_CURRENT_TEST" not in os.environ


def _hard_sandbox_command() -> list[str] | None:
    raw = os.environ.get(_HARD_SANDBOX_ENV, "").strip()
    if not raw:
        return None
    command = shlex.split(raw, posix=os.name != "nt")
    if command and not any(
        Path(part).name.lower() == "run_hard_sandbox.py" for part in command
    ):
        raise ValueError("hard sandbox command must use the shipped runner")
    return command or None


def _parse_worker_result(stdout: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {"error": {"code": "bad_worker_output", "message": "unparseable worker output."}}
    return value if isinstance(value, dict) else {
        "error": {"code": "bad_worker_output", "message": "worker output must be an object."}
    }


def _verify_hard_isolation(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return result
    usage = result.get("resource_usage")
    isolation = usage.get("isolation") if isinstance(usage, dict) else {}
    isolation = isolation if isinstance(isolation, dict) else {}
    missing = sorted(name for name in _REQUIRED_ISOLATION if isolation.get(name) is not True)
    if missing:
        return {
            "error": {
                "code": "sandbox_attestation_failed",
                "message": "hard sandbox did not attest: " + ", ".join(missing),
            }
        }
    return result


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

    try:
        hard_command = _hard_sandbox_command()
    except ValueError:
        return {
            "error": {
                "code": "sandbox_unavailable",
                "message": "the configured hard sandbox command is invalid.",
            }
        }
    if _production_requires_hard_sandbox() and hard_command is None:
        return {
            "error": {
                "code": "sandbox_unavailable",
                "message": (
                    "production adaptation execution requires a configured "
                    "hard sandbox runner."
                ),
            }
        }

    worker = Path(worker_path) if worker_path else _WORKER
    if hard_command is None and not worker.exists():
        raise AdaptationExecutionError("worker_missing", "the sandbox worker is not deployed.")

    # ONLY the code, the bytes, and the template cross the boundary -- no credential,
    # no connection, no environment secret is handed to the worker.
    request = json.dumps({
        "py_source": py_source,
        "input_bytes_b64": base64.b64encode(input_bytes).decode("ascii"),
        "template": template or {},
        "wall_seconds": int(wall_seconds),
    })

    command = hard_command or [sys.executable, str(worker)]
    child_env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOOROW_ADAPTATION_WALL_SECONDS": str(max(1, int(wall_seconds))),
    }
    image = os.environ.get("TOOROW_ADAPTATION_SANDBOX_IMAGE")
    if hard_command is not None and image:
        child_env["TOOROW_ADAPTATION_SANDBOX_IMAGE"] = image
    try:
        proc = subprocess.run(
            command,
            input=request,
            capture_output=True,
            text=True,
            timeout=max(1, int(wall_seconds)) + 2,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": {
                "code": "wall_time_exceeded",
                "message": "the adaptation exceeded the wall-time limit.",
            }
        }
    except OSError:
        return {
            "error": {
                "code": "sandbox_unavailable" if hard_command else "worker_missing",
                "message": "the configured adaptation worker could not be started.",
            }
        }

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"error": {"code": "worker_crash", "message": "the sandbox worker crashed."}}
    result = _parse_worker_result(proc.stdout)
    return _verify_hard_isolation(result) if hard_command is not None else result


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
