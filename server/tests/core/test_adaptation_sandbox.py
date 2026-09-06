"""toorow -- tests for the isolated adaptation sandbox (Story 22.21 / 22.22, AD-3).

Exercises the REAL out-of-process worker via the executor (subprocess), proving:
  - a well-formed adaptation runs and returns canonical rows + resource_usage;
  - a forbidden import (os/socket/subprocess) is refused with a bounded error;
  - the filesystem escape `open` is unavailable;
  - the ATTRIBUTE-WALK escapes are refused: removing builtin NAMES is not enough,
    because `().__class__.__base__.__subclasses__()` walks to any loaded class and
    `__init__.__globals__` hands back the REAL builtins dict and `sys.modules`;
  - a runaway adaptation is killed by the wall-time guard;
  - output-shape / entrypoint faults return bounded errors, never a traceback;
  - the MCP register hook is catalog-consistent (Story 22.22).

The escape tests below prove REACHABILITY ONLY -- they show that a forbidden object
is (or is no longer) obtainable. They never open a file, never touch the network, and
never call anything they reach.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types

from core.adaptation_executor import (
    _WORKER,
    AdaptationExecutionError,
    execute_adaptation_preview,
    run_adaptation,
)


def _load_worker():
    """Import infra/sandbox/worker.py directly (it is not on the server package path)."""
    spec = importlib.util.spec_from_file_location("_sandbox_worker_under_test", _WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_GOOD = """
def adapt(input_bytes, template):
    rows, rejected = [], []
    for i, line in enumerate(input_bytes.decode("utf-8").splitlines()):
        if i == 0:
            continue
        vendor, spend = line.split(",")
        rows.append({"mdm_channel": vendor, "mdm_cost": spend})
    return {"rows": rows, "rejected": rejected}
"""
_DATA = b"Vendor,Spend\nGoogle,100\nMeta,200"


def test_worker_runs_adaptation_and_returns_rows():
    out = run_adaptation(_GOOD, _DATA, {})
    assert "error" not in out
    assert out["rows"] == [
        {"mdm_channel": "Google", "mdm_cost": "100"},
        {"mdm_channel": "Meta", "mdm_cost": "200"},
    ]
    assert out["resource_usage"]["row_count"] == 2


def test_forbidden_import_is_refused():
    for module in ("os", "socket", "subprocess", "pathlib", "importlib", "sys"):
        src = f"import {module}\ndef adapt(i, t):\n    return {{'rows': [], 'rejected': []}}"
        out = run_adaptation(src, b"", {})
        assert out["error"]["code"] == "forbidden_import", module


def test_filesystem_escape_open_is_unavailable():
    src = (
        "def adapt(i, t):\n"
        "    open('/etc/passwd')\n"
        "    return {'rows': [], 'rejected': []}"
    )
    out = run_adaptation(src, b"", {})
    # `open` is removed from builtins -> a bounded adaptation_error, never a file read.
    assert out["error"]["code"] == "adaptation_error"


# --- Attribute-walk escapes (the defect: blocked NAMES, unblocked ATTRIBUTES) -------
# Each source below reaches a forbidden object WITHOUT ever writing a blocked name and
# WITHOUT calling what it reaches. They must be refused before execution.

_ESCAPE_TO_REAL_BUILTINS = """
def adapt(input_bytes, template):
    reached = []
    for cls in ().__class__.__base__.__subclasses__():
        g = cls.__init__.__globals__ if hasattr(cls.__init__, "__globals__") else None
        if not isinstance(g, dict):
            continue
        real = g.get("__builtins__")
        names = real if isinstance(real, dict) else getattr(real, "__dict__", {})
        if "open" in names and "__import__" in names:
            reached = sorted(n for n in ("open", "eval", "exec", "compile", "__import__")
                             if n in names)
            break
    return {"rows": [{"mdm_reached": ",".join(reached)}], "rejected": []}
"""

_ESCAPE_TO_SYS_MODULES = """
def adapt(input_bytes, template):
    found = []
    for cls in ().__class__.__base__.__subclasses__():
        g = cls.__init__.__globals__ if hasattr(cls.__init__, "__globals__") else None
        if not isinstance(g, dict) or "sys" not in g:
            continue
        mods = getattr(g["sys"], "modules", None)
        if isinstance(mods, dict) and "json" in mods:
            found = sorted(n for n in ("os", "socket", "subprocess", "sys") if n in mods)
            break
    return {"rows": [{"mdm_modules": ",".join(found)}], "rejected": []}
"""

_ESCAPE_VIA_COMPUTED_GETATTR = """
def adapt(input_bytes, template):
    name = "__cl" + "ass__"
    cls = getattr((), name)
    return {"rows": [{"mdm_cls": cls.__name__}], "rejected": []}
"""


def test_attribute_walk_to_real_builtins_is_refused():
    # `().__class__.__base__.__subclasses__()` -> any loaded class -> `__init__.__globals__`
    # -> the REAL builtins dict (open/eval/exec/compile/__import__), none of whose names
    # the adaptation ever typed. Reachability only: nothing reached is called.
    out = run_adaptation(_ESCAPE_TO_REAL_BUILTINS, b"", {})
    assert "error" in out, f"sandbox escape: real builtins reachable -> {out.get('rows')}"
    assert out["error"]["code"] == "forbidden_attribute", out["error"]


def test_attribute_walk_to_sys_modules_is_refused():
    # The worker imports `sys` itself, so `sys.modules` -- and therefore `os`, `socket`,
    # `subprocess` once loaded -- is reachable by the same walk. Reachability only.
    out = run_adaptation(_ESCAPE_TO_SYS_MODULES, b"", {})
    assert "error" in out, f"sandbox escape: sys.modules reachable -> {out.get('rows')}"
    assert out["error"]["code"] == "forbidden_attribute", out["error"]


def test_computed_getattr_attribute_escape_is_refused():
    # A dunder assembled at runtime defeats any source-text check that only reads literals.
    out = run_adaptation(_ESCAPE_VIA_COMPUTED_GETATTR, b"", {})
    assert "error" in out, f"sandbox escape: computed getattr worked -> {out.get('rows')}"
    assert out["error"]["code"] == "forbidden_dynamic_attribute", out["error"]


def test_literal_dunder_getattr_is_refused():
    src = (
        "def adapt(i, t):\n"
        "    return {'rows': [{'mdm_c': str(getattr((), '__class__'))}], 'rejected': []}\n"
    )
    out = run_adaptation(src, b"", {})
    assert "error" in out, f"sandbox escape: literal dunder getattr worked -> {out.get('rows')}"
    assert out["error"]["code"] == "forbidden_attribute", out["error"]


def test_bare_dunder_name_is_refused():
    src = (
        "def adapt(i, t):\n"
        "    return {'rows': [{'mdm_b': str(list(__builtins__))}], 'rejected': []}\n"
    )
    out = run_adaptation(src, b"", {})
    assert "error" in out, f"sandbox escape: __builtins__ nameable -> {out.get('rows')}"
    assert out["error"]["code"] == "forbidden_name", out["error"]


def test_frame_attribute_walk_is_refused():
    # Frame/code attributes are NOT dunders (`gi_frame`, `f_globals`, `f_back`), so a
    # dunder-only rule would leave this door open.
    src = (
        "def adapt(i, t):\n"
        "    def _g():\n"
        "        yield 1\n"
        "    g = _g().gi_frame.f_globals\n"
        "    return {'rows': [{'mdm_g': str(sorted(g))}], 'rejected': []}\n"
    )
    out = run_adaptation(src, b"", {})
    assert "error" in out, f"sandbox escape: frame globals reachable -> {out.get('rows')}"
    assert out["error"]["code"] == "forbidden_attribute", out["error"]


def test_refusal_is_typed_and_bounded_not_a_crash():
    out = run_adaptation(_ESCAPE_TO_REAL_BUILTINS, b"", {})
    assert set(out) == {"error"}
    assert set(out["error"]) == {"code", "message"}
    assert len(out["error"]["message"]) <= 500
    assert "Traceback" not in out["error"]["message"]


def test_ordinary_attribute_access_still_works():
    # The guard must refuse the escape CLASS, not ordinary Python.
    src = (
        "def adapt(i, t):\n"
        "    text = i.decode('utf-8').strip().upper()\n"
        "    parts = [p.strip() for p in text.split(',')]\n"
        "    return {'rows': [{'mdm_v': p} for p in parts], 'rejected': []}\n"
    )
    out = run_adaptation(src, b" a, b ", {})
    assert "error" not in out, out
    assert out["rows"] == [{"mdm_v": "A"}, {"mdm_v": "B"}]


def test_class_definition_is_supported():
    # An LLM-authored adaptation routinely defines a helper class or dataclass; the
    # sandbox must not fail it for a missing `__build_class__`.
    src = (
        "class _Row:\n"
        "    def __init__(self, v):\n"
        "        self.value = v\n"
        "\n"
        "def adapt(i, t):\n"
        "    return {'rows': [{'mdm_v': _Row('x').value}], 'rejected': []}\n"
    )
    out = run_adaptation(src, b"", {})
    assert "error" not in out, out
    assert out["rows"] == [{"mdm_v": "x"}]


def test_adaptation_print_does_not_corrupt_the_protocol():
    # `print` in the worker writes to the same stdout that carries the result JSON.
    src = (
        "def adapt(i, t):\n"
        "    print('noise on stdout')\n"
        "    return {'rows': [], 'rejected': []}\n"
    )
    out = run_adaptation(src, b"", {})
    assert "error" not in out, out
    assert any("noise on stdout" in line for line in out["logs"]), out["logs"]


def test_resource_usage_states_whether_os_limits_are_armed():
    # Honesty requirement: on a platform without `resource` (Windows) the ABSENCE of
    # OS limits must be reported, never silently assumed.
    out = run_adaptation(_GOOD, _DATA, {})
    limits = out["resource_usage"]["os_limits"]
    assert isinstance(limits["armed"], bool)
    assert limits["platform"]
    if limits["armed"]:
        assert limits["applied"]
    else:
        assert limits["reason"]


def _fake_resource_module(*, refuse: set[str] | None = None) -> types.ModuleType:
    """A stand-in for the POSIX `resource` module, which does not exist on Windows."""
    refuse = refuse or set()
    module = types.ModuleType("resource")
    module.RLIM_INFINITY = -1
    module.applied = {}
    for index, name in enumerate((
        "RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE",
        "RLIMIT_NPROC", "RLIMIT_CORE",
    )):
        setattr(module, name, index)
    ids = {index: name for index, name in enumerate((
        "RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE",
        "RLIMIT_NPROC", "RLIMIT_CORE",
    ))}

    def getrlimit(rid):
        return (-1, -1)

    def setrlimit(rid, limits):
        name = ids[rid]
        if name in refuse:
            raise OSError("operation not permitted")
        module.applied[name] = limits[0]

    module.getrlimit = getrlimit
    module.setrlimit = setrlimit
    return module


def test_posix_rlimits_are_armed_when_the_platform_has_them(monkeypatch):
    # This repository runs on Windows, where `resource` does not exist -- without this
    # stub the POSIX branch would ship never having executed once.
    worker = _load_worker()
    fake = _fake_resource_module()
    monkeypatch.setitem(sys.modules, "resource", fake)
    report = worker._arm_os_limits(memory_bytes=256 * 1024 * 1024, cpu_seconds=5)
    assert report["armed"] is True
    assert report["applied"] == {
        "RLIMIT_AS": 256 * 1024 * 1024,
        "RLIMIT_CPU": 5,
        "RLIMIT_FSIZE": 0,
        "RLIMIT_NOFILE": worker.DEFAULT_MAX_OPEN_FILES,
        "RLIMIT_NPROC": 0,
        "RLIMIT_CORE": 0,
    }
    assert report["unavailable"] == {}
    # Even when armed, the report must keep naming what rlimits do NOT cover.
    assert report["never_enforced_here"]


def test_a_refused_rlimit_is_reported_not_swallowed(monkeypatch):
    worker = _load_worker()
    fake = _fake_resource_module(refuse={"RLIMIT_NPROC"})
    monkeypatch.setitem(sys.modules, "resource", fake)
    report = worker._arm_os_limits(memory_bytes=1024 * 1024, cpu_seconds=1)
    assert report["armed"] is True
    assert report["unavailable"] == {"RLIMIT_NPROC": "OSError"}


def test_absent_resource_module_is_stated_not_assumed(monkeypatch):
    worker = _load_worker()
    monkeypatch.setitem(sys.modules, "resource", None)  # `import resource` -> ImportError
    report = worker._arm_os_limits(memory_bytes=1024 * 1024, cpu_seconds=1)
    assert report["armed"] is False
    assert "resource" in report["reason"]
    assert report["applied"] == {}


def test_runaway_adaptation_hits_wall_time():
    src = "def adapt(i, t):\n    while True:\n        pass\n"
    out = run_adaptation(src, b"", {}, wall_seconds=1)
    assert out["error"]["code"] == "wall_time_exceeded"


def test_undeclared_non_test_runtime_fails_closed(monkeypatch):
    for name in (
        "TOOROW_ENVIRONMENT",
        "TOOROW_ENV",
        "ENVIRONMENT",
        "APP_ENV",
        "PYTEST_CURRENT_TEST",
        "TOOROW_ADAPTATION_SANDBOX_COMMAND",
    ):
        monkeypatch.delenv(name, raising=False)
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_unavailable"


def test_standard_toorow_production_environment_requires_hard_runner(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    monkeypatch.delenv("TOOROW_ADAPTATION_SANDBOX_COMMAND", raising=False)
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_unavailable"


def test_production_refuses_the_language_only_worker(monkeypatch):
    monkeypatch.setenv("TOOROW_ENV", "production")
    monkeypatch.delenv("TOOROW_ADAPTATION_SANDBOX_COMMAND", raising=False)
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_unavailable"


def test_hard_runner_must_attest_every_kernel_boundary(monkeypatch):
    monkeypatch.setenv("TOOROW_ENV", "production")
    monkeypatch.setenv("TOOROW_ADAPTATION_SANDBOX_COMMAND", "python /app/run_hard_sandbox.py")
    result = {"rows": [], "rejected": [], "logs": [], "resource_usage": {}}
    monkeypatch.setattr(
        "core.adaptation_executor.subprocess.run",
        lambda *args, **kwargs: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(result), stderr=""
        ),
    )
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_attestation_failed"


def test_hard_runner_attestation_allows_production_execution(monkeypatch):
    from core.adaptation_executor import _REQUIRED_ISOLATION

    monkeypatch.setenv("TOOROW_ENV", "production")
    monkeypatch.setenv("TOOROW_ADAPTATION_SANDBOX_COMMAND", "python /app/run_hard_sandbox.py")
    result = {
        "rows": [],
        "rejected": [],
        "logs": [],
        "resource_usage": {
            "isolation": {name: True for name in _REQUIRED_ISOLATION}
        },
    }
    monkeypatch.setattr(
        "core.adaptation_executor.subprocess.run",
        lambda *args, **kwargs: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(result), stderr=""
        ),
    )
    out = run_adaptation(_GOOD, _DATA, {})
    assert "error" not in out


def test_missing_entrypoint_is_bounded():
    out = run_adaptation("x = 1\n", b"", {})
    assert out["error"]["code"] == "missing_entrypoint"


def test_bad_output_shape_is_bounded():
    src = "def adapt(i, t):\n    return 'not a dict'\n"
    out = run_adaptation(src, b"", {})
    assert out["error"]["code"] == "bad_output_shape"


def test_empty_source_raises_executor_precondition():
    try:
        run_adaptation("", b"", {})
        raise AssertionError("expected AdaptationExecutionError")
    except AdaptationExecutionError as exc:
        assert exc.code == "empty_source"


def test_preview_bounds_rows_and_reports_counts():
    src = (
        "def adapt(i, t):\n"
        "    return {'rows': [{'mdm_x': str(n)} for n in range(250)], 'rejected': []}\n"
    )
    preview = execute_adaptation_preview(src, b"", {}, preview_limit=100)
    assert preview["ok"] is True
    assert len(preview["rows"]) == 100
    assert preview["row_count"] == 250
    assert preview["truncated"] is True


def test_preview_surfaces_bounded_error():
    preview = execute_adaptation_preview("import os\ndef adapt(i,t): return {}", b"", {})
    assert preview["ok"] is False
    assert preview["error"]["code"] == "forbidden_import"


def test_mcp_register_is_catalog_consistent():
    # Story 22.22: register() attaches a valid, consistent capability declaration
    # (operations / read / operational / none) so validate_catalog passes at boot.
    from core.adaptation_executor_mcp import register

    class _FakeMcp:
        def tool(self, handler=None, **_kw):
            return handler

    # register_profiled asserts consistency before recording; a raise would fail here.
    register(_FakeMcp())


def test_base64_sample_roundtrip_into_worker():
    # The MCP tool ships a base64 sample; prove the executor decodes + runs it.
    encoded = base64.b64encode(_DATA).decode("ascii")
    out = run_adaptation(_GOOD, base64.b64decode(encoded), {})
    assert out["resource_usage"]["row_count"] == 2

def test_worker_rejects_non_object_rows_with_a_bounded_error():
    out = run_adaptation(
        "def adapt(i, t):\n    return {'rows': [1], 'rejected': []}\n",
        b"",
        {},
    )
    assert out["error"]["code"] == "bad_output_shape"


def test_whitespace_environment_fails_closed_outside_pytest(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "   ")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TOOROW_ADAPTATION_SANDBOX_COMMAND", raising=False)
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_unavailable"


def test_malformed_hard_runner_command_is_bounded(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    monkeypatch.setenv("TOOROW_ADAPTATION_SANDBOX_COMMAND", '"unterminated')
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_unavailable"


def test_malformed_isolation_attestation_is_refused(monkeypatch):
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "TOOROW_ADAPTATION_SANDBOX_COMMAND", "python /app/run_hard_sandbox.py"
    )
    monkeypatch.setattr(
        "core.adaptation_executor.subprocess.run",
        lambda *args, **kwargs: types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"rows": [], "resource_usage": {"isolation": []}}),
            stderr="",
        ),
    )
    out = run_adaptation(_GOOD, _DATA, {})
    assert out["error"]["code"] == "sandbox_attestation_failed"
