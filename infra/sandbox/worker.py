"""toorow -- Isolated adaptation sandbox worker (Story 22.21, AD-3).

A dedicated, out-of-process worker that runs an LLM-authored ``.py`` adaptation and
returns ONLY ``{rows, rejected, logs, resource_usage}``. The MCP server (which holds
DB credentials) NEVER ``exec``/``eval``s adaptation code in-process: it spawns THIS
script as a separate process (see ``core.adaptation_executor``) and communicates over
stdin/stdout.

--------------------------------------------------------------------------------
WHAT THIS MODULE GUARANTEES
--------------------------------------------------------------------------------
1. The adaptation never runs inside the credentialed server process. The parent ships
   only ``{py_source, input_bytes, template}``; no credential, connection or secret
   crosses the boundary, and the child is started with a stripped environment.
2. The submitted source is REJECTED BEFORE EXECUTION (static AST check, see
   ``_validate_source_ast``) if it contains:
     * any dunder ATTRIBUTE access (``__class__``, ``__base__``, ``__bases__``,
       ``__subclasses__``, ``__globals__``, ``__mro__``, ``__dict__``, ``__code__``...);
     * any dunder NAME reference (``__builtins__``, ``__import__``, ``__loader__``...);
     * any frame/code attribute, which are NOT dunders and would otherwise be an open
       door (``gi_frame``, ``cr_frame``, ``f_globals``, ``f_back``, ``f_code``,
       ``tb_frame``, ``mro``...);
     * ``getattr``/``setattr``/``delattr``/``hasattr`` with a NON-LITERAL name (a dunder
       assembled at runtime defeats any text-level check), or with a literal that is
       itself forbidden; and a bare reference to those functions (passing ``getattr``
       to ``map`` would launder the same escape).
   This closes the attribute-walk escape that removing builtin NAMES alone does not:
   ``().__class__.__base__.__subclasses__()`` reaches every loaded class, and
   ``SomeClass.__init__.__globals__`` hands back the REAL ``__builtins__`` dict (with
   ``open``/``eval``/``exec``/``__import__``) and this worker's own ``sys``, hence
   ``sys.modules`` -- without the adaptation ever typing a blocked name.
   Every rejection is a TYPED, bounded error (``forbidden_attribute``,
   ``forbidden_name``, ``forbidden_dynamic_attribute``), never a crash or a traceback.
3. ``import`` is admitted only for a small pure-computation whitelist (json/math/
   datetime/decimal/re/base64/csv/collections/itertools/statistics/unicodedata/string);
   anything else is ``forbidden_import``.
4. ``open``/``eval``/``exec``/``compile``/``input``/``breakpoint``/``help``/``vars``/
   ``globals``/``locals``/``memoryview``/``exit``/``quit``/``license`` are absent from
   the adaptation's builtins.
5. ``print`` is captured into ``logs`` (bounded) instead of writing to the stdout that
   carries the result JSON.
6. Output is bounded (row count + serialized size), and the run is bounded in time: a
   SIGALRM alarm here on POSIX, plus the PARENT subprocess timeout, which is the
   authoritative wall-time guard on every OS.
7. On POSIX ONLY, OS limits are armed before execution via ``resource.setrlimit``:
   RLIMIT_AS (address space), RLIMIT_CPU, RLIMIT_FSIZE=0 (no regular-file writes),
   RLIMIT_NOFILE, RLIMIT_NPROC=0 (no new process), RLIMIT_CORE=0.

--------------------------------------------------------------------------------
WHAT THIS MODULE DOES **NOT** GUARANTEE -- READ THIS BEFORE TRUSTING IT
--------------------------------------------------------------------------------
* **No network isolation.** Nothing here can prevent a socket from being opened. The
  import guard and the attribute guard make it hard to REACH the socket API from the
  adaptation namespace, but that is a language-level obstacle, not a kernel one. Real
  network denial belongs to the container/namespace layer (no egress, no resolver).
* **No filesystem namespace.** RLIMIT_FSIZE=0 stops writes to regular files on POSIX;
  it stops NOTHING on Windows, and it never prevents READS anywhere. Real filesystem
  denial belongs to the container layer (read-only rootfs, empty mount namespace).
* **No OS limits at all on Windows.** The ``resource`` module does not exist there.
  This repository is developed on Windows: locally, memory/file/process limits are NOT
  in force. That absence is REPORTED, never assumed -- see ``resource_usage.os_limits``
  in every successful result (``armed: false`` plus a ``reason``).
* **No defense against a CPython interpreter bug.** A restricted-namespace sandbox in
  pure Python is a hardening layer, not a security boundary. It has historically been
  bypassed by novel introspection paths; the static guard above enumerates the paths we
  know. The boundary that must hold is the process/container one.
* **The AST guard rejects some legitimate code.** ``super().__init__()``, ``obj.__dict__``
  and any deliberate introspection are refused with a typed error. Class definitions,
  including ``def __init__`` inside a class body, DO work (``__build_class__`` is
  provided); only dunder *access* is refused, not dunder *definition*.
* **RLIMIT_FSIZE=0 means the worker itself cannot write a regular file**: redirecting
  its stdout to a file (rather than the parent's pipe) would fail on POSIX.

Remaining isolation (network namespace, mount namespace, seccomp/syscall filter, user
namespace, cgroup memory/CPU on every OS) is the deployable's infra concern -- AD-3
records the tech as deferred. This module is the worker CONTRACT plus a portable
reference enforcement. It imports nothing from ``server/`` so it can run in a minimal,
credential-free environment.
"""

from __future__ import annotations

import ast
import base64
import json
import sys
import time

# Modules an adaptation may import (pure computation only -- no I/O, net, or process).
_ALLOWED_IMPORTS = frozenset({
    "json", "math", "datetime", "decimal", "re", "base64", "csv",
    "collections", "itertools", "statistics", "unicodedata", "string",
})

# Non-dunder attributes that walk to a frame, a code object or the type graph. Blocking
# only dunders would leave these open: `gen.gi_frame.f_globals`, `tb.tb_frame`,
# `type(x).mro()` are all escape starts spelled without a single underscore pair.
_FORBIDDEN_ATTRS = frozenset({
    "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await",
    "ag_frame", "ag_code", "f_globals", "f_locals", "f_builtins", "f_back", "f_code",
    "f_trace", "tb_frame", "tb_next", "func_globals", "func_code", "func_builtins",
    "mro",
})

# Attribute-by-name functions: admitted ONLY as a direct call with a literal str name.
_ATTR_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr"})

# Builtins removed from the adaptation namespace.
_BLOCKED_BUILTINS = frozenset({
    "open", "exec", "eval", "compile", "input", "breakpoint", "help",
    "__import__", "globals", "locals", "vars", "memoryview",
    "exit", "quit", "copyright", "credits", "license",
})

# Output bounds (defense against a runaway adaptation).
MAX_ROWS = 1_000_000
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
DEFAULT_WALL_SECONDS = 10
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_OPEN_FILES = 32

# Captured-print bounds.
MAX_LOG_LINES = 200
MAX_LOG_CHARS = 500


class SandboxError(Exception):
    """A bounded, non-leaking sandbox failure (carries a stable code)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _is_dunder(name: str) -> bool:
    return isinstance(name, str) and name.startswith("__") and name.endswith("__")


def _reject_attribute(name: str) -> None:
    """Raise a typed refusal for an attribute name that opens the introspection graph."""
    if _is_dunder(name):
        raise SandboxError(
            "forbidden_attribute",
            f"attribute '{name}' is not accessible in the adaptation sandbox "
            "(dunder attributes walk to the interpreter internals).",
        )
    if name in _FORBIDDEN_ATTRS:
        raise SandboxError(
            "forbidden_attribute",
            f"attribute '{name}' is not accessible in the adaptation sandbox "
            "(frame/code/type-graph attribute).",
        )


def _visit(node: ast.AST) -> None:
    """Depth-first, document-order validation of one AST node and its children."""
    if isinstance(node, ast.Attribute):
        _reject_attribute(node.attr)
    elif isinstance(node, ast.Name):
        if _is_dunder(node.id):
            raise SandboxError(
                "forbidden_name",
                f"the name '{node.id}' is not referenceable in the adaptation sandbox.",
            )
        if node.id in _ATTR_FUNCS:
            raise SandboxError(
                "forbidden_dynamic_attribute",
                f"'{node.id}' may only be called directly with a literal attribute name.",
            )
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        for declared in node.names:
            if _is_dunder(declared):
                raise SandboxError(
                    "forbidden_name",
                    f"the name '{declared}' is not referenceable in the adaptation sandbox.",
                )
    elif isinstance(node, ast.alias) and _is_dunder(node.asname or ""):
        raise SandboxError(
            "forbidden_name",
            f"the alias '{node.asname}' is not permitted in the adaptation sandbox.",
        )

    # getattr/setattr/delattr/hasattr: literal-name call form only.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _ATTR_FUNCS:
        name_arg = node.args[1] if len(node.args) > 1 else None
        if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
            raise SandboxError(
                "forbidden_dynamic_attribute",
                f"'{node.func.id}' requires a literal attribute name in the adaptation "
                "sandbox (a computed name defeats the static guard).",
            )
        _reject_attribute(name_arg.value)
        for child in list(node.args) + [kw.value for kw in node.keywords]:
            _visit(child)  # deliberately skips node.func (already validated as a call)
        return

    for child in ast.iter_child_nodes(node):
        _visit(child)


def _validate_source_ast(py_source: str) -> None:
    """Refuse the attribute-walk escape class BEFORE the source is compiled or run."""
    try:
        tree = ast.parse(py_source)
    except SyntaxError as exc:
        raise SandboxError("compile_error", f"SyntaxError: {exc.msg}") from exc
    except (ValueError, MemoryError, RecursionError) as exc:
        raise SandboxError("compile_error", f"{type(exc).__name__}: source is not parseable.") \
            from exc
    try:
        _visit(tree)
    except SandboxError:
        raise
    except RecursionError as exc:  # pathologically nested source
        raise SandboxError("compile_error", "source nesting is too deep to validate.") from exc


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in _ALLOWED_IMPORTS:
        raise SandboxError(
            "forbidden_import",
            f"import of '{name}' is not permitted in the adaptation sandbox.",
        )
    return __import__(name, globals, locals, fromlist, level)


def _captured_print(logs: list[str]):
    """A `print` that appends to the bounded `logs` list.

    The real `print` writes to the SAME stdout that carries the result JSON: one debug
    print by an LLM-authored adaptation would make the whole run unparseable.
    """

    def _print(*args, sep=" ", end="\n", file=None, flush=False):  # noqa: ARG001
        if len(logs) >= MAX_LOG_LINES:
            return
        try:
            line = str(sep).join(str(a) for a in args)
        except Exception:  # noqa: BLE001 -- a hostile __str__ must not break the run
            line = "<unprintable>"
        logs.append(line[:MAX_LOG_CHARS])

    return _print


def _safe_builtins(logs: list[str]) -> dict:
    """A builtins dict with I/O / introspection escapes removed."""
    import builtins as _b  # the worker itself may use builtins; the adaptation gets a subset

    safe = {
        k: getattr(_b, k)
        for k in dir(_b)
        if k not in _BLOCKED_BUILTINS and not k.startswith("__")
    }
    safe["__import__"] = _guarded_import
    safe["print"] = _captured_print(logs)
    # `class X:` compiles to an implicit __build_class__ lookup. It cannot be NAMED by
    # the adaptation (the AST guard refuses dunder names), so providing it re-enables
    # ordinary class/dataclass definitions without widening the reachable surface.
    safe["__build_class__"] = _b.__build_class__
    return safe


def _bounded(rows: list, rejected: list) -> None:
    if not isinstance(rows, list) or not isinstance(rejected, list):
        raise SandboxError("bad_output_shape", "adapt() must return lists for rows/rejected.")
    if any(not isinstance(row, dict) for row in rows):
        raise SandboxError("bad_output_shape", "every adapted row must be an object.")
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


def _arm_os_limits(*, memory_bytes: int, cpu_seconds: int) -> dict:
    """Arm POSIX rlimits; ALWAYS report what was armed and what could not be.

    Portable by construction: ``resource`` does not exist on Windows, so the import is
    conditional and the absence is stated in the returned report (``armed: false`` plus
    a human-readable ``reason``). Silence here would be the original defect inverted.
    """
    report: dict = {
        "armed": False,
        "platform": sys.platform,
        "applied": {},
        "unavailable": {},
        "never_enforced_here": [
            "network isolation (no egress control is possible from pure Python)",
            "filesystem namespace (rlimits cap writes, they do not hide the tree)",
            "syscall filtering (seccomp) and cgroup accounting",
        ],
    }
    try:
        import resource  # noqa: PLC0415 -- POSIX only; absent on Windows by design
    except ImportError:
        report["reason"] = (
            f"the 'resource' module does not exist on platform '{sys.platform}': NO OS-level "
            "memory / file-size / process / open-file limit is in force in this worker. Only "
            "the parent subprocess timeout and the in-process guards apply."
        )
        return report

    wanted = (
        ("RLIMIT_AS", int(memory_bytes)),
        ("RLIMIT_CPU", max(1, int(cpu_seconds))),
        ("RLIMIT_FSIZE", 0),
        ("RLIMIT_NOFILE", DEFAULT_MAX_OPEN_FILES),
        ("RLIMIT_NPROC", 0),
        ("RLIMIT_CORE", 0),
    )
    for name, want in wanted:
        rid = getattr(resource, name, None)
        if rid is None:
            report["unavailable"][name] = "unsupported on this platform"
            continue
        try:
            _current_soft, hard = resource.getrlimit(rid)
            soft = want if (hard == resource.RLIM_INFINITY or want <= hard) else hard
            resource.setrlimit(rid, (soft, hard))
            report["applied"][name] = soft
        except (ValueError, OSError) as exc:
            report["unavailable"][name] = type(exc).__name__

    report["armed"] = bool(report["applied"])
    if not report["armed"]:
        report["reason"] = "no rlimit could be applied on this platform."
    return report


def execute_adaptation(
    py_source: str,
    input_bytes: bytes,
    template: dict,
    *,
    wall_seconds: int = DEFAULT_WALL_SECONDS,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    arm_os_limits: bool = False,
) -> dict:
    """Run one adaptation in isolation and return {rows, rejected, logs, resource_usage}.

    The adaptation source MUST define ``adapt(input_bytes, template) -> dict`` with
    ``{"rows": [...], "rejected": [...]}``. Any failure (forbidden attribute/name/import,
    bad shape, exception, limit breach) returns a bounded ``{"error": {code, message}}``
    -- no traceback or host detail leaks.

    ``arm_os_limits`` is False by default so that importing this module in a test process
    cannot clamp that process; ``main()`` (the real worker process) passes True.
    """
    logs: list[str] = []
    started = time.monotonic()
    limits = (
        _arm_os_limits(memory_bytes=memory_bytes, cpu_seconds=wall_seconds)
        if arm_os_limits
        else {"armed": False, "platform": sys.platform, "applied": {}, "unavailable": {},
              "reason": "os limits not requested by this caller (in-process invocation)."}
    )
    _install_alarm(wall_seconds)
    try:
        if not isinstance(py_source, str) or "adapt" not in py_source:
            raise SandboxError(
                "missing_entrypoint", "adaptation must define adapt(input_bytes, template)."
            )

        # Static refusal BEFORE compile/exec: the escape must never get to run.
        _validate_source_ast(py_source)

        sandbox_globals: dict = {"__builtins__": _safe_builtins(logs), "__name__": "adaptation"}
        try:
            code = compile(py_source, "<adaptation>", "exec")
            exec(code, sandbox_globals)  # noqa: S102 -- runs in THIS isolated worker, never the server
        except SandboxError:
            raise
        except MemoryError as exc:
            raise SandboxError(
                "memory_limit_exceeded", "adaptation exceeded the memory limit."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxError("compile_error", f"{type(exc).__name__}: {exc}") from exc

        adapt = sandbox_globals.get("adapt")
        if not callable(adapt):
            raise SandboxError("missing_entrypoint", "adapt is not callable.")

        try:
            out = adapt(input_bytes, template)
        except SandboxError:
            raise
        except MemoryError as exc:
            raise SandboxError(
                "memory_limit_exceeded", "adaptation exceeded the memory limit."
            ) from exc
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
            "resource_usage": {
                "wall_ms": wall_ms,
                "row_count": len(rows),
                "os_limits": limits,
            },
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
        memory_bytes=int(request.get("memory_bytes", DEFAULT_MEMORY_BYTES)),
        arm_os_limits=True,
    )
    sys.stdout.write(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
