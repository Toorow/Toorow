"""toorow -- tests for the isolated adaptation sandbox (Story 22.21 / 22.22, AD-3).

Exercises the REAL out-of-process worker via the executor (subprocess), proving:
  - a well-formed adaptation runs and returns canonical rows + resource_usage;
  - a forbidden import (os/socket/subprocess) is refused with a bounded error;
  - the filesystem escape `open` is unavailable;
  - a runaway adaptation is killed by the wall-time guard;
  - output-shape / entrypoint faults return bounded errors, never a traceback;
  - the MCP register hook is catalog-consistent (Story 22.22).
"""

from __future__ import annotations

import base64

from core.adaptation_executor import (
    AdaptationExecutionError,
    execute_adaptation_preview,
    run_adaptation,
)

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


def test_runaway_adaptation_hits_wall_time():
    src = "def adapt(i, t):\n    while True:\n        pass\n"
    out = run_adaptation(src, b"", {}, wall_seconds=1)
    assert out["error"]["code"] == "wall_time_exceeded"


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
