"""The platform-clock MCP tools: declared, gated, and honest about drift.

Fully offline. `core.platform_clocks` does not have to exist for these to run:
the seam is replaced by a fake, the connection is a MagicMock, and no GCP call is
ever attempted.

What is pinned here, and why each one:

  (a) the five tools go through `register_profiled` with their profile, effect and
      confirmation mode -- and NO bare `mcp.tool` appears in the module. A bare
      registration escapes `CapabilityProfileMiddleware` entirely, which is a
      known live hole on `submit_feedback`; this test breaks if it is reproduced.
  (b) drift is RENDERED, never repaired: a read calls `observe` + `reconcile` and
      never `apply` or `run_now`.
  (c) an unobservable GCP yields `unknown`, never `in_sync` -- an observation that
      did not run has proven nothing.
  (d) a caller without a platform role is refused, and the seam is never touched.
  (e) the three writes refuse without the echoed clock name; the run tool in
      particular cannot fire by accident.

File paths are anchored on `Path(__file__).resolve().parents[3]` (the repository
root), never on the working directory: AI-136 is the class defect where a
relative path made the same assertion render two different verdicts depending on
where pytest was launched from.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_MODULE_PATH = REPO_ROOT / "server" / "core" / "platform_clocks_mcp.py"

PLATFORM_ADMIN = "ops@example.com"
OUTSIDER = "someone@example.com"

TOOL_NAMES = {
    "list_platform_clocks",
    "get_platform_clock",
    "set_platform_clock_cadence",
    "apply_platform_clock",
    "run_platform_clock_now",
}


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """Empty catalog, platform allow-list set, high-risk profiles opted in."""
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", PLATFORM_ADMIN)
    # The read model reads project/region from the environment; unset here so the
    # seam is called with exactly the arguments the test asserts on.
    monkeypatch.delenv("TOOROW_GCP_PROJECT", raising=False)
    monkeypatch.delenv("TOOROW_GCP_REGION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_REGION", raising=False)
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


class _Recorder:
    """A stand-in mcp that records every `tool()` registration verbatim."""

    def __init__(self):
        self.tool_calls: list[tuple[str, dict]] = []

    def tool(self, handler, **kwargs):
        name = kwargs.get("name") or handler.__name__
        self.tool_calls.append((name, kwargs))
        return SimpleNamespace(
            name=name, tags=kwargs.get("tags"), meta=kwargs.get("meta")
        )


def _register(monkeypatch) -> tuple[_Recorder, dict]:
    """Run `platform_clocks_mcp.register` and return (recorder, handlers-by-name)."""
    from core import mcp_profiles, platform_clocks_mcp

    recorder = _Recorder()
    handlers: dict = {}
    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        handlers[kwargs.get("name") or handler.__name__] = handler
        return real(mcp, handler, **kwargs)

    monkeypatch.setattr(mcp_profiles, "register_profiled", spy)
    platform_clocks_mcp.register(recorder)
    return recorder, handlers


class _Conn:
    def __init__(self):
        self.commit = MagicMock()
        self.rollback = MagicMock()


@contextmanager
def _connection():
    yield _Conn()


def _seam(**overrides) -> SimpleNamespace:
    """A fake `core.platform_clocks` with the five-function interface."""
    declared = [
        {"name": "dispatch-nightly", "schedule": "0 2 * * *"},
        {"name": "drain-outbox", "schedule": "*/5 * * * *"},
    ]
    observed = [{"name": "dispatch-nightly", "schedule": "0 3 * * *"}]
    verdicts = {"dispatch-nightly": "drifted", "drain-outbox": "missing_in_gcp"}
    seam = SimpleNamespace(
        list_declared=MagicMock(return_value=declared),
        observe=MagicMock(return_value=observed),
        reconcile=MagicMock(return_value=verdicts),
        apply=MagicMock(return_value={"action": "updated"}),
        run_now=MagicMock(return_value={"dispatched": True}),
        set_declared=MagicMock(return_value={"schedule": "0 4 * * *"}),
    )
    for key, value in overrides.items():
        setattr(seam, key, value)
    return seam


@contextmanager
def _environment(seam, identity=PLATFORM_ADMIN):
    """Patch the seam, the connection and the MCP identity for one call."""
    from core import platform_clocks_mcp, platform_clocks_read_model

    with (
        patch.object(platform_clocks_read_model, "_seam", return_value=seam),
        patch.object(platform_clocks_mcp, "_identity", return_value=identity),
        patch("core.db.get_connection", side_effect=_connection),
    ):
        yield


def _data(result) -> dict:
    """Unwrap the AD-1 envelope a tool returns."""
    return result.structured_content["data"]


# ---------------------------------------------------------------------------
# (a) every tool is declared to the capability catalog -- no bare mcp.tool.
# ---------------------------------------------------------------------------


def test_the_five_tools_are_declared_with_profile_effect_and_confirmation(monkeypatch):
    from core import mcp_profiles

    _register(monkeypatch)
    declarations = {d.name: d for d in mcp_profiles.registered_declarations()}

    assert set(declarations) == TOOL_NAMES
    assert all(d.profile == "operations" for d in declarations.values())
    assert all(d.data_class == "operational" for d in declarations.values())

    for name in ("list_platform_clocks", "get_platform_clock"):
        assert declarations[name].effect == "read"
        assert declarations[name].confirmation_mode == "none"

    for name in (
        "set_platform_clock_cadence",
        "apply_platform_clock",
        "run_platform_clock_now",
    ):
        assert declarations[name].effect == "confirmed_write", (
            f"{name} touches platform infrastructure; a read declaration would let "
            "the middleware treat it as a safe call"
        )
        assert declarations[name].confirmation_mode == "human"

    # The catalog validator must accept the whole set (no contradiction).
    assert len(mcp_profiles.validate_catalog()) == len(TOOL_NAMES)


def test_every_registration_carries_capability_metadata(monkeypatch):
    """A registration without `meta` is a tool the middleware cannot filter."""
    recorder, _handlers = _register(monkeypatch)

    assert {name for name, _kwargs in recorder.tool_calls} == TOOL_NAMES
    for name, kwargs in recorder.tool_calls:
        meta = kwargs.get("meta") or {}
        missing = {
            "profile",
            "effect",
            "data_class",
            "confirmation_mode",
        } - set(meta)
        assert not missing, f"{name} registered without {sorted(missing)}"


def test_the_module_never_registers_a_bare_tool():
    """The regression guard for the `submit_feedback` class of defect.

    A bare `mcp.tool(...)` escapes `CapabilityProfileMiddleware` completely: no
    profile filtering at discovery, no denial at call time. This reads the source
    from the repository root rather than the working directory (AI-136).
    """
    source = MCP_MODULE_PATH.read_text(encoding="utf-8")
    assert "mcp.tool(" not in source, (
        "a bare mcp.tool registration escapes the capability middleware; "
        "use register_profiled"
    )
    assert source.count("register_profiled(") >= len(TOOL_NAMES)


# ---------------------------------------------------------------------------
# (b)/(c) drift is rendered, never repaired -- and never optimistically.
# ---------------------------------------------------------------------------


def test_listing_renders_drift_and_repairs_nothing(monkeypatch):
    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam):
        data = _data(handlers["list_platform_clocks"]())

    verdicts = {c["clock_name"]: c["verdict"] for c in data["clocks"]}
    assert verdicts == {
        "dispatch-nightly": "drifted",
        "drain-outbox": "missing_in_gcp",
    }
    assert data["out_of_sync"] == ["dispatch-nightly", "drain-outbox"]
    assert data["drift_is_reported_not_repaired"] is True
    assert data["observation"]["reachable"] is True

    seam.observe.assert_called_once()
    seam.reconcile.assert_called_once()
    seam.apply.assert_not_called()
    seam.run_now.assert_not_called()
    seam.set_declared.assert_not_called()


def test_an_unobservable_gcp_is_unknown_and_never_in_sync(monkeypatch):
    _recorder, handlers = _register(monkeypatch)
    seam = _seam(observe=MagicMock(side_effect=RuntimeError("no credential")))

    with _environment(seam):
        data = _data(handlers["list_platform_clocks"]())

    assert [c["verdict"] for c in data["clocks"]] == ["unknown", "unknown"]
    assert all(c["in_sync"] is False for c in data["clocks"])
    assert data["observation"]["reachable"] is False
    assert data["observation"]["error"] == "RuntimeError"
    # reconcile must not be asked for a verdict over an observation that failed.
    seam.reconcile.assert_not_called()


def test_reading_one_clock_never_invents_it(monkeypatch):
    _recorder, handlers = _register(monkeypatch)
    seam = _seam()
    from fastmcp.exceptions import ToolError

    with _environment(seam), pytest.raises(ToolError):
        handlers["get_platform_clock"]("poll-health")

    with _environment(seam):
        data = _data(handlers["get_platform_clock"]("dispatch-nightly"))
    assert data["clock"]["clock_name"] == "dispatch-nightly"
    assert data["clock"]["verdict"] == "drifted"
    seam.apply.assert_not_called()


# ---------------------------------------------------------------------------
# (d) the platform role is required, and its absence touches nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_platform_clocks", ()),
        ("get_platform_clock", ("dispatch-nightly",)),
        ("set_platform_clock_cadence", ("dispatch-nightly", "dispatch-nightly")),
        ("apply_platform_clock", ("dispatch-nightly", "dispatch-nightly")),
        ("run_platform_clock_now", ("dispatch-nightly", "dispatch-nightly")),
    ],
)
def test_a_caller_without_a_platform_role_is_refused(monkeypatch, tool, args):
    """Project membership proves nothing about a platform clock. Fail closed."""
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam, identity=OUTSIDER), pytest.raises(ToolError) as excinfo:
        handlers[tool](*args)

    # Existence-hiding: the refusal never says "forbidden".
    assert json.loads(str(excinfo.value))["code"] == "not_found"
    seam.list_declared.assert_not_called()
    seam.observe.assert_not_called()
    seam.apply.assert_not_called()
    seam.run_now.assert_not_called()
    seam.set_declared.assert_not_called()


def test_an_unset_allow_list_refuses_everyone(monkeypatch):
    """Deny-by-default: an unconfigured deployment exposes these to nobody."""
    from fastmcp.exceptions import ToolError

    monkeypatch.delenv("TOOROW_SUPER_ADMINS", raising=False)
    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["list_platform_clocks"]()
    assert json.loads(str(excinfo.value))["code"] == "not_found"
    seam.list_declared.assert_not_called()


# ---------------------------------------------------------------------------
# (e) the writes name their target twice.
# ---------------------------------------------------------------------------


def test_running_a_clock_requires_the_echoed_confirmation(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["run_platform_clock_now"]("dispatch-nightly", "")
    assert json.loads(str(excinfo.value))["code"] == "confirmation_required"
    seam.run_now.assert_not_called()

    with _environment(seam), pytest.raises(ToolError):
        handlers["run_platform_clock_now"]("dispatch-nightly", "drain-outbox")
    seam.run_now.assert_not_called()

    with _environment(seam):
        data = _data(
            handlers["run_platform_clock_now"]("dispatch-nightly", "dispatch-nightly")
        )
    assert data["clock_name"] == "dispatch-nightly"
    seam.run_now.assert_called_once()
    assert seam.run_now.call_args.kwargs["clock_name"] == "dispatch-nightly"
    assert seam.run_now.call_args.kwargs["actor"] == PLATFORM_ADMIN


def test_applying_requires_confirmation_and_returns_the_resulting_state(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["apply_platform_clock"]("dispatch-nightly", "")
    assert json.loads(str(excinfo.value))["code"] == "confirmation_required"
    seam.apply.assert_not_called()

    with _environment(seam):
        data = _data(
            handlers["apply_platform_clock"]("dispatch-nightly", "dispatch-nightly")
        )
    seam.apply.assert_called_once()
    # The state is RE-COLLECTED after applying: the verdict obtained, not hoped for.
    assert data["state"]["clocks"][0]["clock_name"] == "dispatch-nightly"
    assert seam.observe.call_count >= 1


def test_editing_the_cadence_writes_the_declaration_only(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["set_platform_clock_cadence"]("dispatch-nightly", "nope", "0 4 * * *")
    assert json.loads(str(excinfo.value))["code"] == "confirmation_required"
    seam.set_declared.assert_not_called()

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["set_platform_clock_cadence"]("dispatch-nightly", "dispatch-nightly")
    assert json.loads(str(excinfo.value))["code"] == "invalid_param"

    with _environment(seam):
        data = _data(
            handlers["set_platform_clock_cadence"](
                "dispatch-nightly", "dispatch-nightly", "0 4 * * *"
            )
        )
    seam.set_declared.assert_called_once()
    assert seam.set_declared.call_args.kwargs["schedule"] == "0 4 * * *"
    # Editing the declaration does NOT reach GCP -- that is what apply is for.
    seam.apply.assert_not_called()
    assert data["applied_to_gcp"] is False


def test_a_missing_declaration_writer_fails_loudly(monkeypatch):
    """No silent no-op: an edit that wrote nowhere must not look successful."""
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seam = _seam()
    del seam.set_declared

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["set_platform_clock_cadence"](
            "dispatch-nightly", "dispatch-nightly", "0 4 * * *"
        )
    assert json.loads(str(excinfo.value))["code"] == "seam_unavailable"


def test_a_malformed_clock_name_never_reaches_the_seam(monkeypatch):
    from fastmcp.exceptions import ToolError

    _recorder, handlers = _register(monkeypatch)
    seam = _seam()

    with _environment(seam), pytest.raises(ToolError) as excinfo:
        handlers["run_platform_clock_now"]("../../etc/passwd", "../../etc/passwd")
    assert json.loads(str(excinfo.value))["code"] == "invalid_param"
    seam.run_now.assert_not_called()
