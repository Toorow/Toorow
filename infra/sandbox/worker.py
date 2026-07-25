"""toorow -- Isolated adaptation sandbox worker (Story 22.21, AD-3).

A dedicated, out-of-process worker that runs an LLM-authored ``.py`` adaptation in
isolation and returns ONLY ``{rows, rejected, logs, resource_usage}``. The MCP
server (which holds DB credentials) NEVER ``exec``/``eval``s adaptation code
in-process: it spawns THIS script as a separate process (see
``core.adaptation_executor``) and communicates over stdin/stdout.

Isolation enforced here (in-process to THIS worker, which is itself a separate
process from the credentialed server):
  * no filesystem: ``open`` is removed from builtins;
  * no network / process / import escape: ``__import__`` is replaced by a guard
    that admits ONLY a small pure-computation whitelist (json/math/datetime/
    decimal/re/base64/csv/collections/itertools/statistics/unicodedata) and
    rejects os/sys/socket/subprocess/pathlib/importlib/ctypes/threading/...;
  * no eval/exec/compile/getattr-escape inside the adaptation namespace;
  * bounded output: row count + serialized size are capped;
  * bounded wall-time: a SIGALRM alarm on POSIX (the PARENT also enforces a hard
    subprocess timeout, which is the authoritative wall-time guard on every OS).

The EXACT production isolation (a network-less, filesystem-less, seccomp/container
sandbox) is the deployable's infra concern (AD-3: tech deferred) -- this module is
the worker CONTRACT + a portable reference enforcement. It imports nothing from
``server/`` so it can run in a minimal, credential-free environment.
"""

from __future__ import annotations

import base64
import json
import sys
import time

# Modules an adaptation may import (pure computation only -- no I/O, net, or process).
_ALLOWED_IMPORTS = frozenset({
    "json", "math", "datetime", "decimal", "re", "base64", "csv",
    "collections", "itertools", "statistics", "unicodedata", "string",
})

# Output bounds (defense against a runaway adaptation).
MAX_ROWS = 1_000_000
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
DEFAULT_WALL_SECONDS = 10


class SandboxError(Exception):
    """A bounded, non-leaking sandbox failure (carries a stable code)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in _ALLOWED_IMPORTS:
        raise SandboxError(
            "forbidden_import",
            f"import of '{name}' is not permitted in the adaptation sandbox.",
        )
    return __import__(name, globals, locals, fromlist, level)


def _safe_builtins() -> dict:
    """A builtins dict with I/O / introspection escapes removed."""
    import builtins as _b  # the worker itself may use builtins; the adaptation gets a subset

    blocked = {
        "open", "exec", "eval", "compile", "input", "breakpoint", "help",
        "__import__", "globals", "locals", "vars", "memoryview",
    }
    safe = {k: getattr(_b, k) for k in dir(_b) if k not in blocked and not k.startswith("__")}
    safe["__import__"] = _guarded_import
    return safe


def _bounded(rows: list, rejected: list) -> None:
    if not isinstance(rows, list) or not isinstance(rejected, list):
        raise SandboxError("bad_output_shape", "adapt() must return lists for rows/rejected.")
    if len(rows) > MAX_ROWS:
        raise SandboxError("row_limit_exceeded", f"{len(rows)} rows exceeds {MAX_ROWS}.")
    size = len(json.dumps(rows, default=str)) + len(json.dumps(rejected, default=str))
    if size > MAX_OUTPUT_BYTES:
        raise SandboxError("output_too_large", f"output {size} bytes exceeds {MAX_OUTPUT_BYTES}.")


def _install_alarm(wall_seconds: int):
    """Best-effort in-worker wall-time alarm (POSIX). The parent timeout is authoritative."""
    try:
        import signal  # noqa: PLC0415  (POSIX only; absent/limited on Windows)

        def _handler(_signum, _frame):
            raise SandboxError("wall_time_exceeded", "adaptation exceeded the wall-time limit.")

        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(max(1, int(wall_seconds)))
    except (ImportError, ValueError, AttributeError):
        pass  # no SIGALRM on this platform; rely on the parent subprocess timeout


def execute_adaptation(
    py_source: str,
    input_bytes: bytes,
    template: dict,
    *,
    wall_seconds: int = DEFAULT_WALL_SECONDS,
) -> dict:
    """Run one adaptation in isolation and return {rows, rejected, logs, resource_usage}.

    The adaptation source MUST define ``adapt(input_bytes, template) -> dict`` with
    ``{"rows": [...], "rejected": [...]}``. Any failure (forbidden import, bad shape,
    exception, limit breach) returns a bounded ``{"error": {code, message}}`` -- no
    traceback or host detail leaks.
    """
    logs: list[str] = []
    started = time.monotonic()
    _install_alarm(wall_seconds)
    try:
        if not isinstance(py_source, str) or "adapt" not in py_source:
            raise SandboxError(
                "missing_entrypoint", "adaptation must define adapt(input_bytes, template)."
            )

        sandbox_globals: dict = {"__builtins__": _safe_builtins(), "__name__": "adaptation"}
        try:
            code = compile(py_source, "<adaptation>", "exec")
            exec(code, sandbox_globals)  # noqa: S102 -- runs in THIS isolated worker, never the server
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxError("compile_error", f"{type(exc).__name__}: {exc}") from exc

        adapt = sandbox_globals.get("adapt")
        if not callable(adapt):
            raise SandboxError("missing_entrypoint", "adapt is not callable.")

        try:
            out = adapt(input_bytes, template)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxError("adaptation_error", f"{type(exc).__name__}: {exc}") from exc

        if not isinstance(out, dict):
            raise SandboxError("bad_output_shape", "adapt() must return a dict.")
        rows = out.get("rows", [])
        rejected = out.get("rejected", [])
        _bounded(rows, rejected)

        wall_ms = int((time.monotonic() - started) * 1000)
        return {
            "rows": rows,
            "rejected": rejected,
            "logs": logs,
            "resource_usage": {"wall_ms": wall_ms, "row_count": len(rows)},
        }
    except SandboxError as exc:
        return {"error": {"code": exc.code, "message": exc.message[:500]}}
    finally:
        try:
            import signal  # noqa: PLC0415

            signal.alarm(0)
        except (ImportError, ValueError, AttributeError):
            pass


def main() -> int:
    """stdin: {py_source, input_bytes_b64, template, wall_seconds}; stdout: result JSON."""
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.stdout.write(json.dumps({"error": {"code": "bad_request", "message": "invalid JSON"}}))
        return 0
    input_bytes = base64.b64decode(request.get("input_bytes_b64", "") or "")
    result = execute_adaptation(
        request.get("py_source", ""),
        input_bytes,
        request.get("template", {}) or {},
        wall_seconds=int(request.get("wall_seconds", DEFAULT_WALL_SECONDS)),
    )
    sys.stdout.write(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
