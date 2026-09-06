"""Story 36.11 capability-catalog and host/workspace governance tests.

Fully offline (no live Postgres): the catalog service is pure and the middleware is
exercised with lightweight stand-in tool/context objects. Mirrors the MagicMock
style of test_epic36_operations.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    """Each test starts from an empty registry and restores it afterwards.

    High-risk profiles are OFF by default (fail-closed, review C1); the "with proof"
    visibility tests in this file opt in explicitly, mirroring a deployment that has
    wired server-side host/workspace verification. One test overrides this back to
    off to prove the default-closed behavior.
    """
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    # The registry these calls empty is PROCESS-GLOBAL, and clearing it on
    # teardown used to leave the catalog empty for every test that ran afterwards.
    # Repaired once, at the harness: see `_restore_mcp_capability_registry` in
    # `tests/conftest.py`. Nothing to add here, and nothing to remove -- this file
    # genuinely wants a known-empty registry while it runs.
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


def test_high_risk_gated_off_by_default_even_with_valid_evidence(monkeypatch):
    """Fail-closed (review C1): a self-reported claim with well-formed evidence must
    NOT unlock high-risk profiles unless the deployment explicitly opts in."""
    from core import mcp_profiles

    monkeypatch.delenv("TOOROW_MCP_HIGHRISK_ENABLED", raising=False)
    grants = {
        "enabled_profiles": ["insights", "operations", "governance", "support"],
        "endpoint_binding": "admin-endpoint",
        "workspace_evidence_hash": "a" * 64,
    }
    visible = mcp_profiles.visible_profiles("user-1", {"host": "opaque"}, grants)
    assert visible == frozenset({"insights"})


def _register(mcp_profiles, mcp, name, profile, effect, data_class, confirmation_mode):
    """Register a no-op handler through the profiled registrar."""

    def handler():  # pragma: no cover - never invoked in these unit tests
        return None

    handler.__name__ = name
    return mcp_profiles.register_profiled(
        mcp,
        handler,
        profile=profile,
        effect=effect,
        data_class=data_class,
        confirmation_mode=confirmation_mode,
    )


# ---------------------------------------------------------------------------
# (a) validate_catalog rejects undeclared / contradictory tools.
# ---------------------------------------------------------------------------


def test_register_rejects_unknown_enum_values():
    from core import mcp_profiles
    from core.mcp_profiles import CatalogValidationError

    with pytest.raises(CatalogValidationError):
        _register(
            mcp_profiles, MagicMock(), "bad", "wizardry", "read", "public", "none"
        )


def test_register_rejects_insights_write_contradiction():
    from core import mcp_profiles
    from core.mcp_profiles import CatalogValidationError

    # Insights must be read-only: a write-effect insights tool fails closed.
    with pytest.raises(CatalogValidationError):
        _register(
            mcp_profiles, MagicMock(), "leaky", "insights", "confirmed_write", "public", "none"
        )


def test_register_rejects_read_tool_that_demands_human_confirmation():
    from core import mcp_profiles
    from core.mcp_profiles import CatalogValidationError

    with pytest.raises(CatalogValidationError):
        _register(
            mcp_profiles, MagicMock(), "weird", "operations", "read", "public", "human"
        )


def test_validate_catalog_flags_a_contradictory_tool_injected_into_registry():
    """A declaration that bypassed the registrar still fails closed at boot."""
    from core import mcp_profiles
    from core.mcp_profiles import CatalogValidationError, ToolDeclaration

    # Inject an insights/mutation contradiction directly into the registry.
    mcp_profiles._REGISTRY.declarations["rogue"] = ToolDeclaration(
        name="rogue",
        profile="insights",
        effect="confirmed_write",
        data_class="public",
        confirmation_mode="none",
    )
    with pytest.raises(CatalogValidationError):
        mcp_profiles.validate_catalog()


def test_validate_catalog_accepts_a_well_formed_catalog():
    from core import mcp_profiles

    mcp = MagicMock()
    _register(mcp_profiles, mcp, "report_read", "insights", "read", "public", "none")
    _register(mcp_profiles, mcp, "pull_retry", "operations", "prepare", "operational", "server")
    _register(mcp_profiles, mcp, "publish", "governance", "confirmed_write", "sensitive", "host")
    decls = mcp_profiles.validate_catalog()
    assert {d.name for d in decls} == {"report_read", "pull_retry", "publish"}
    # register_profiled must have tagged + meta-annotated the FastMCP tool.
    call = mcp.tool.call_args_list[0]
    assert "profile:insights" in call.kwargs["tags"]
    assert call.kwargs["meta"]["confirmation_mode"] == "none"


# ---------------------------------------------------------------------------
# (b) default discovery yields Insights only; higher profiles are hidden.
# ---------------------------------------------------------------------------


def _tool(name, profile):
    return SimpleNamespace(name=name, meta={"profile": profile}, tags={f"profile:{profile}"})


def test_default_discovery_lists_insights_only(monkeypatch):
    from core import mcp_profiles

    mw = mcp_profiles.build_middleware()
    assert mw is not None

    tools = [
        _tool("report_read", "insights"),
        _tool("pull_retry", "operations"),
        _tool("publish", "governance"),
        _tool("support_probe", "support"),
    ]

    async def call_next(_context):
        return tools

    # No token / no capability context -> insights only (fail closed).
    monkeypatch.setattr(mcp_profiles, "_capability_context", lambda: ("anonymous", {}, {}))
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == ["report_read"]


def test_an_undeclared_tool_is_visible_to_nobody(monkeypatch):
    """AD-43 -- this test used to assert the OPPOSITE, and that is why it changed.

    It read: "an undeclared tool is treated as the Insights default and stays
    listed". Insights is the profile that is always visible, so that default
    bounded exactly half the surface. Measured on the assembled catalog on
    2026-08-12: 83 of the 92 tools a default host saw carried no declaration --
    39 of them named after a connector, twelve of them tools that WRITE.

    An undeclared tool is now in no caller's visible set. This is the runtime
    backstop only: `assert_every_tool_is_declared` refuses to BOOT with one, so a
    tool a client calls today gets a declaration rather than a disappearance."""
    from core import mcp_profiles

    mw = mcp_profiles.build_middleware()
    undeclared = SimpleNamespace(name="get_card", meta=None, tags=set())
    declared_read = _tool("report_read", "insights")
    declared_ops = _tool("pull_retry", "operations")

    async def call_next(_context):
        return [undeclared, declared_read, declared_ops]

    monkeypatch.setattr(mcp_profiles, "_capability_context", lambda: ("anonymous", {}, {}))
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == ["report_read"]
    assert mcp_profiles._tool_profile(undeclared) is None


def test_verified_context_reveals_enabled_high_risk_profiles(monkeypatch):
    from core import mcp_profiles

    mw = mcp_profiles.build_middleware()
    tools = [
        _tool("report_read", "insights"),
        _tool("pull_retry", "operations"),
        _tool("publish", "governance"),
    ]

    async def call_next(_context):
        return tools

    grants = {
        # 67-16: only an ATTESTED context (a live app.mcp_capability_contexts row,
        # read by `_capability_context`) can carry this key, and only it unlocks a
        # high-risk profile. A fabricated claim of the same shape buys Insights.
        "attested_context_id": "mcpctx_TESTATTESTED",
        "enabled_profiles": ["insights", "operations"],
        "endpoint_binding": "admin-endpoint",
        "workspace_evidence_hash": "a" * 64,
    }
    monkeypatch.setattr(
        mcp_profiles, "_capability_context", lambda: ("user-1", {"host": "opaque"}, grants)
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    # operations opted-in and evidence-backed => visible; governance stays hidden.
    assert [t.name for t in result] == ["report_read", "pull_retry"]


def test_missing_workspace_evidence_keeps_high_risk_hidden():
    from core import mcp_profiles

    # enabled_profiles asks for operations, but no workspace_evidence_hash => fail closed.
    grants = {"enabled_profiles": ["insights", "operations"], "endpoint_binding": "e"}
    visible = mcp_profiles.visible_profiles("user-1", {"host": "opaque"}, grants)
    assert visible == frozenset({"insights"})


def test_no_host_ordering_only_capability_drives_visibility():
    """E36-NFR03: visibility is identical regardless of the opaque host name."""
    from core import mcp_profiles

    grants = {
        "attested_context_id": "mcpctx_TESTATTESTED",
        "enabled_profiles": ["insights", "governance"],
        "endpoint_binding": "e",
        "workspace_evidence_hash": "b" * 64,
    }
    a = mcp_profiles.visible_profiles("user-1", {"host": "host-alpha"}, grants)
    b = mcp_profiles.visible_profiles("user-1", {"host": "host-beta"}, grants)
    assert a == b == frozenset({"insights", "governance"})


# ---------------------------------------------------------------------------
# (c) a direct call to a hidden tool is denied at call time (AC6).
# ---------------------------------------------------------------------------


def test_direct_call_to_hidden_tool_is_denied(monkeypatch):
    from core import mcp_profiles

    mcp = MagicMock()
    _register(mcp_profiles, mcp, "publish", "governance", "confirmed_write", "sensitive", "host")

    mw = mcp_profiles.build_middleware()
    call_next = MagicMock()  # must NOT be awaited when denied

    # Authenticated but no opt-in for governance -> insights only.
    monkeypatch.setattr(
        mcp_profiles,
        "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    context = SimpleNamespace(message=SimpleNamespace(name="publish"))
    with pytest.raises(Exception) as exc:
        asyncio.run(mw.on_call_tool(context, call_next))
    assert "Tool not found." in str(exc.value)
    call_next.assert_not_called()


def test_direct_call_to_insights_tool_is_allowed(monkeypatch):
    from core import mcp_profiles

    mcp = MagicMock()
    _register(mcp_profiles, mcp, "report_read", "insights", "read", "public", "none")

    mw = mcp_profiles.build_middleware()

    async def call_next(_context):
        return "ok"

    monkeypatch.setattr(mcp_profiles, "_capability_context", lambda: ("user-1", {}, {}))
    context = SimpleNamespace(message=SimpleNamespace(name="report_read"))
    assert asyncio.run(mw.on_call_tool(context, call_next)) == "ok"


# ---------------------------------------------------------------------------
# (d) catalog_version is deterministic and read-catalog is stable across admin churn.
# ---------------------------------------------------------------------------


def test_catalog_version_is_deterministic_and_read_stable_across_admin_churn():
    from core import mcp_profiles

    mcp = MagicMock()
    _register(mcp_profiles, mcp, "report_read", "insights", "read", "public", "none")
    _register(mcp_profiles, mcp, "coverage_read", "insights", "read", "operational", "none")

    read_before = mcp_profiles.catalog_version("read")
    admin_before = mcp_profiles.catalog_version("admin")

    # Deterministic: same registry -> same hash (no time/random).
    assert mcp_profiles.catalog_version("read") == read_before

    # Add an admin tool -> read version MUST NOT change (E36-NFR04); admin changes.
    _register(mcp_profiles, mcp, "pull_retry", "operations", "prepare", "operational", "server")
    assert mcp_profiles.catalog_version("read") == read_before
    assert mcp_profiles.catalog_version("admin") != admin_before

    # Remove the admin tool -> read version still stable; admin returns to original.
    del mcp_profiles._REGISTRY.declarations["pull_retry"]
    assert mcp_profiles.catalog_version("read") == read_before
    assert mcp_profiles.catalog_version("admin") == admin_before


def test_catalog_version_rejects_unknown_connection_class():
    from core import mcp_profiles

    with pytest.raises(ValueError):
        mcp_profiles.catalog_version("everything")


# ---------------------------------------------------------------------------
# (e) migration 067 carries the mcp_capability_contexts contract.
# ---------------------------------------------------------------------------


def test_migration_067_contains_capability_context_contract():
    from tests.conftest import REPO_ROOT

    sql = (REPO_ROOT / "infra/nango/migrations/067_capability_catalogs.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS app.mcp_capability_contexts" in sql
    assert "endpoint_binding" in sql
    assert "enabled_profiles" in sql
    assert "workspace_evidence_hash" in sql
    assert "policy_version" in sql
    assert "catalog_version" in sql
    assert "immutable" in sql
    # Immutable binding is enforced by a trigger (AC3).
    assert "binding is immutable" in sql
    assert "REFERENCES app.organizations(id) ON DELETE RESTRICT" in sql
