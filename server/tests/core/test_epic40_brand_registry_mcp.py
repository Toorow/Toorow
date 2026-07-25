"""Story 40.5 brand-registry admin MCP tests (Governance profile, AI-56 seam + handlers).

Fully offline (no live Postgres): the guards and the 40.1-40.3 stores are monkeypatched,
the DB is never opened, and the capability middleware is driven with lightweight stand-in
tool/context objects. Mirrors test_epic36_governed_publication.py verbatim for the harness.

Coverage:
  (AI-56 seam) registration + validate_catalog accepts them; every tool profile=governance;
      read tools effect=read/confirmation=none, write tools effect=write/confirmation=human;
      hidden without opt-in; visible with a governance opt-in; a direct call to a hidden
      tool is DENIED via on_call_tool (fail-closed-if-hidden).
  (handlers) mutation delegation + provenance stamping (created_by/identity); alias
      add/remove; wire binding (opaque source); role set; alert approve (Epic 13 path);
      match confirm/correct (alias + negative alias); guard fail-closed (store never
      called); cross-org/cross-project refusal (not_found / cross_project_denied); read
      scoping (org/project only, no sibling leak); missing_param; AD-2 no-vocabulary grep.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # High-risk profiles are fail-closed OFF by default; this suite exercises the
    # Governance profile, so it opts in like a deployment with server-side host/workspace
    # verification (exactly like test_epic36_governed_publication.py).
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


_READ_TOOLS = {"registry_entities", "registry_project_roles", "registry_new_entity_alerts"}
_WRITE_TOOLS = {
    "registry_entity_upsert",
    "registry_alias_add",
    "registry_alias_remove",
    "registry_wire_binding",
    "registry_binding_remove",
    "registry_role_set",
    "registry_match_confirm",
    "registry_match_correct",
    "registry_alert_approve",
}
_ALL_TOOLS = _READ_TOOLS | _WRITE_TOOLS


class _Recorder:
    def __init__(self):
        self.handlers = {}
        self.tool = MagicMock()


def _register_all(recorder):
    from core import brand_registry_mcp, mcp_profiles

    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.handlers[handler.__name__] = handler
        return real(mcp, handler, **kwargs)

    mcp_profiles.register_profiled = spy  # type: ignore[assignment]
    try:
        brand_registry_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real  # type: ignore[assignment]
    return recorder.handlers


def _handlers():
    return _register_all(_Recorder())


def _tool(name, profile):
    return SimpleNamespace(name=name, meta={"profile": profile}, tags={f"profile:{profile}"})


def _err_code(exc: Exception) -> str:
    import json

    from fastmcp.exceptions import ToolError

    assert isinstance(exc, ToolError)
    return json.loads(str(exc))["code"]


# ---------------------------------------------------------------------------
# AI-56 seam -- registration + validate_catalog (AC1, AC7).
# ---------------------------------------------------------------------------


def test_registry_tools_register_and_validate_catalog_accepts_them():
    from core import mcp_profiles

    handlers = _handlers()
    assert set(handlers) == _ALL_TOOLS
    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert set(decls) == _ALL_TOOLS
    assert all(d.profile == "governance" for d in decls.values())
    validated = {d.name for d in mcp_profiles.validate_catalog()}
    assert validated == _ALL_TOOLS


def test_read_tools_are_read_none_and_write_tools_are_write_human():
    from core import mcp_profiles

    _handlers()
    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    for name in _READ_TOOLS:
        assert decls[name].effect == "read"
        assert decls[name].confirmation_mode == "none"
    for name in _WRITE_TOOLS:
        assert decls[name].effect == "write"
        assert decls[name].confirmation_mode == "human"


def test_every_tool_is_sensitive_governance():
    from core import mcp_profiles

    _handlers()
    for d in mcp_profiles.registered_declarations():
        assert d.profile == "governance"
        assert d.data_class == "sensitive"


# ---------------------------------------------------------------------------
# AI-56 seam -- discovery gating + deny-if-hidden (AC1, AC2).
# ---------------------------------------------------------------------------


def test_registry_tools_hidden_without_opt_in(monkeypatch):
    from core import mcp_profiles

    _handlers()
    mw = mcp_profiles.build_middleware()
    tools = [_tool("report_read", "insights")] + [
        _tool(name, "governance") for name in sorted(_ALL_TOOLS)
    ]

    async def call_next(_context):
        return tools

    monkeypatch.setattr(
        mcp_profiles,
        "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == ["report_read"]


def test_registry_tools_visible_with_governance_opt_in(monkeypatch):
    from core import mcp_profiles

    _handlers()
    mw = mcp_profiles.build_middleware()
    tools = [_tool("report_read", "insights")] + [
        _tool(name, "governance") for name in sorted(_ALL_TOOLS)
    ]

    async def call_next(_context):
        return tools

    grants = {
        "enabled_profiles": ["insights", "governance"],
        "endpoint_binding": "admin-endpoint",
        "workspace_evidence_hash": "a" * 64,
    }
    monkeypatch.setattr(
        mcp_profiles, "_capability_context", lambda: ("user-1", {"host": "opaque"}, grants)
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    visible = {t.name for t in result}
    assert _ALL_TOOLS.issubset(visible)
    assert "report_read" in visible


def test_direct_call_to_registry_entity_upsert_denied_without_opt_in(monkeypatch):
    from core import mcp_profiles

    _handlers()
    mw = mcp_profiles.build_middleware()
    call_next = MagicMock()
    monkeypatch.setattr(
        mcp_profiles,
        "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    context = SimpleNamespace(message=SimpleNamespace(name="registry_entity_upsert"))
    with pytest.raises(Exception) as exc:
        asyncio.run(mw.on_call_tool(context, call_next))
    assert "introuvable" in str(exc.value)
    call_next.assert_not_called()


def test_high_risk_off_hides_tools_even_with_governance_opt_in(monkeypatch):
    from core import mcp_profiles

    _handlers()
    monkeypatch.delenv("TOOROW_MCP_HIGHRISK_ENABLED", raising=False)
    mw = mcp_profiles.build_middleware()
    tools = [
        _tool("report_read", "insights"),
        _tool("registry_entity_upsert", "governance"),
    ]

    async def call_next(_context):
        return tools

    grants = {
        "enabled_profiles": ["insights", "governance"],
        "endpoint_binding": "admin-endpoint",
        "workspace_evidence_hash": "a" * 64,
    }
    monkeypatch.setattr(
        mcp_profiles, "_capability_context", lambda: ("user-1", {"host": "opaque"}, grants)
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == ["report_read"]


# ---------------------------------------------------------------------------
# Offline handlers -- mutation delegation + provenance (AC3, AC4).
# ---------------------------------------------------------------------------


def _patch_identity(monkeypatch, identity="user-1"):
    from core import brand_registry_mcp as brm

    monkeypatch.setattr(brm, "_identity", lambda: identity)
    monkeypatch.setattr(brm, "_guard_org_manage", lambda org, ident: None)
    monkeypatch.setattr(brm, "_guard_org_read", lambda org, ident: None)
    monkeypatch.setattr(brm, "_assert_project_in_org", lambda project, org: None)


def test_entity_upsert_delegates_and_stamps_created_by(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    captured = {}

    def fake_upsert(**kwargs):
        captured.update(kwargs)
        return {"id": "tent_1", **kwargs}

    monkeypatch.setattr(reg, "upsert_tracked_entity", fake_upsert)

    result = handlers["registry_entity_upsert"]("org-1", "Acme", aliases=["a"])
    assert captured["created_by"] == "camille"
    assert captured["org_id"] == "org-1"
    assert captured["canonical_name"] == "Acme"
    assert result.structured_content["data"]["entity"]["id"] == "tent_1"


def test_alias_add_guards_entity_in_org_then_delegates(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    order = []
    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: order.append(("assert", e, o)))
    monkeypatch.setattr(
        reg,
        "add_alias",
        lambda e, a, *, identity: order.append(("add", e, a, identity)) or {"id": e},
    )

    handlers["registry_alias_add"]("tent_1", "org-1", "Acme Inc")
    assert order[0] == ("assert", "tent_1", "org-1")
    assert order[1] == ("add", "tent_1", "Acme Inc", "camille")


def test_alias_remove_delegates_with_identity(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    captured = {}
    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: None)
    monkeypatch.setattr(
        reg,
        "remove_alias",
        lambda e, a, *, identity: captured.update({"e": e, "a": a, "id": identity}) or {"id": e},
    )
    handlers["registry_alias_remove"]("tent_1", "org-1", "Acme Inc")
    assert captured == {"e": "tent_1", "a": "Acme Inc", "id": "camille"}


def test_wire_binding_delegates_opaque_source_and_created_by(monkeypatch):
    from core import entity_source_bindings as binding
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: None)
    captured = {}

    def fake_upsert(**kwargs):
        captured.update(kwargs)
        return {"id": "bind_1", **kwargs}

    monkeypatch.setattr(binding, "upsert_source_binding", fake_upsert)

    handlers["registry_wire_binding"]("tent_1", "org-1", "some-opaque-source", external_id="ext-9")
    assert captured["source"] == "some-opaque-source"  # passed through opaque
    assert captured["external_id"] == "ext-9"
    assert captured["created_by"] == "camille"
    assert captured["entity_id"] == "tent_1"


def test_binding_remove_guards_binding_in_org_then_deletes(monkeypatch):
    from core import entity_source_bindings as binding

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    order = []
    monkeypatch.setattr(
        binding, "assert_binding_in_org", lambda b, o: order.append(("assert", b, o))
    )
    monkeypatch.setattr(
        binding,
        "delete_source_binding",
        lambda b, *, identity: order.append(("del", b, identity)) or True,
    )
    handlers["registry_binding_remove"]("bind_1", "org-1")
    assert order[0] == ("assert", "bind_1", "org-1")
    assert order[1] == ("del", "bind_1", "camille")


def test_role_set_delegates_with_created_by(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    captured = {}

    def fake_set(**kwargs):
        captured.update(kwargs)
        return {"id": "epr_1", **kwargs}

    monkeypatch.setattr(reg, "set_entity_project_role", fake_set)
    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: None)  # existence-hiding guard

    handlers["registry_role_set"]("tent_1", "proj-1", "org-1", "competitor")
    assert captured["created_by"] == "camille"
    assert captured["role"] == "competitor"
    assert captured["entity_id"] == "tent_1"
    assert captured["project_id"] == "proj-1"


def test_alert_approve_delegates_epic13_draft_to_approved(monkeypatch):
    from core import tracked_entity_matching as match

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    captured = {}

    def fake_approve(**kwargs):
        captured.update(kwargs)
        return {
            "alert": {"id": kwargs["alert_id"], "status": "approved"},
            "entity": {"id": "tent_9"},
        }

    monkeypatch.setattr(match, "approve_new_entity_alert", fake_approve)

    result = handlers["registry_alert_approve"]("alert_1", "org-1", "Acme")
    assert captured["identity"] == "camille"  # approver provenance
    assert captured["alert_id"] == "alert_1"
    assert captured["canonical_name"] == "Acme"
    assert captured["org_id"] == "org-1"
    assert result.structured_content["data"]["entity"]["id"] == "tent_9"


def test_match_confirm_delegates_correct_entity_alias(monkeypatch):
    from core import tracked_entity_matching as match

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    captured = {}

    def fake_correct(**kwargs):
        captured.update(kwargs)
        return {"kind": "alias", "entity": {"id": kwargs.get("correct_entity_id")}}

    monkeypatch.setattr(match, "correct_match", fake_correct)

    handlers["registry_match_confirm"]("org-1", "proj-1", "dec_1", "Acme Co", "tent_1")
    assert captured["correct_entity_id"] == "tent_1"
    assert "negative_for_entity_id" not in captured
    assert captured["identity"] == "camille"
    assert captured["decision_id"] == "dec_1"
    assert captured["raw_string"] == "Acme Co"
    assert captured["project_id"] == "proj-1"


def test_match_correct_alias_vs_negative_alias(monkeypatch):
    from core import tracked_entity_matching as match

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    captured = {}

    def fake_correct(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return {"kind": "ok"}

    monkeypatch.setattr(match, "correct_match", fake_correct)

    handlers["registry_match_correct"]("org-1", "proj-1", "dec_1", "Acme", "alias", "tent_1")
    assert captured["correct_entity_id"] == "tent_1"
    assert "negative_for_entity_id" not in captured

    handlers["registry_match_correct"](
        "org-1", "proj-1", "dec_1", "Acme", "negative_alias", "tent_2"
    )
    assert captured["negative_for_entity_id"] == "tent_2"
    assert "correct_entity_id" not in captured
    assert captured["identity"] == "camille"


def test_match_correct_rejects_unknown_correction_kind(monkeypatch):
    from core import tracked_entity_matching as match

    handlers = _handlers()
    _patch_identity(monkeypatch, "camille")
    called = MagicMock()
    monkeypatch.setattr(match, "correct_match", called)
    with pytest.raises(Exception) as exc:
        handlers["registry_match_correct"]("org-1", "proj-1", "dec_1", "Acme", "bogus", "tent_1")
    assert _err_code(exc.value) == "invalid_param"
    called.assert_not_called()


# ---------------------------------------------------------------------------
# Offline -- guard fail-closed + cross-project/cross-org refusal (AC5).
# ---------------------------------------------------------------------------


def test_guard_manage_failure_refuses_and_never_calls_store(monkeypatch):
    from core import brand_registry_mcp as brm
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    monkeypatch.setattr(brm, "_identity", lambda: "user-1")

    def deny(org, ident):
        raise brm._tool_error("forbidden", "Droits insuffisants.")

    monkeypatch.setattr(brm, "_guard_org_manage", deny)
    store = MagicMock()
    monkeypatch.setattr(reg, "upsert_tracked_entity", store)

    with pytest.raises(Exception) as exc:
        handlers["registry_entity_upsert"]("org-1", "Acme")
    assert _err_code(exc.value) == "forbidden"
    store.assert_not_called()


def test_entity_not_found_surfaces_not_found(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch)

    def raise_nf(e, o):
        raise reg.EntityNotFound("absent")

    monkeypatch.setattr(reg, "assert_entity_in_org", raise_nf)
    monkeypatch.setattr(reg, "add_alias", MagicMock())

    with pytest.raises(Exception) as exc:
        handlers["registry_alias_add"]("tent_x", "org-1", "Acme")
    assert _err_code(exc.value) == "not_found"


def test_cross_project_denied_surfaces_typed_refusal(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch)

    def raise_cpd(**kwargs):
        raise reg.CrossProjectDenied("entity org != project org")

    # assert_entity_in_org passes (in-org entity); this exercises the generic CrossProjectDenied
    # -> cross_project_denied mapping. (A FOREIGN entity is now existence-hidden to not_found by
    # the guard -- proven in the 40.6 isolation suite.)
    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: None)
    monkeypatch.setattr(reg, "set_entity_project_role", raise_cpd)

    with pytest.raises(Exception) as exc:
        handlers["registry_role_set"]("tent_1", "proj-1", "org-1", "own")
    assert _err_code(exc.value) == "cross_project_denied"


def test_invalid_role_surfaces_invalid_param(monkeypatch):
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    _patch_identity(monkeypatch)

    def raise_ir(**kwargs):
        raise reg.InvalidRole("bad role")

    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: None)  # existence guard passes
    monkeypatch.setattr(reg, "set_entity_project_role", raise_ir)

    with pytest.raises(Exception) as exc:
        handlers["registry_role_set"]("tent_1", "proj-1", "org-1", "nope")
    assert _err_code(exc.value) == "invalid_param"


def test_project_roles_project_in_org_failure_is_not_found(monkeypatch):
    from core import brand_registry_mcp as brm
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    monkeypatch.setattr(brm, "_identity", lambda: "user-1")
    monkeypatch.setattr(brm, "_guard_org_read", lambda org, ident: None)

    def deny(project, org):
        raise brm._tool_error("not_found", "Projet introuvable.")

    monkeypatch.setattr(brm, "_assert_project_in_org", deny)
    store = MagicMock()
    monkeypatch.setattr(reg, "list_project_roles", store)

    with pytest.raises(Exception) as exc:
        handlers["registry_project_roles"]("org-1", "proj-foreign")
    assert _err_code(exc.value) == "not_found"
    store.assert_not_called()


# ---------------------------------------------------------------------------
# Offline -- read scoping + missing_param + AD-2 (AC7).
# ---------------------------------------------------------------------------


def test_registry_entities_org_scoped_delegation(monkeypatch):
    from core import brand_registry_mcp as brm
    from core import tracked_entity_registry as reg

    handlers = _handlers()
    monkeypatch.setattr(brm, "_identity", lambda: "user-1")
    monkeypatch.setattr(brm, "_guard_org_read", lambda org, ident: None)
    captured = {}
    monkeypatch.setattr(
        reg,
        "list_tracked_entities_by_org",
        lambda org, *, status: captured.update({"org": org, "status": status})
        or [{"id": "tent_1"}],
    )
    result = handlers["registry_entities"]("org-1")
    assert captured == {"org": "org-1", "status": None}
    assert result.structured_content["data"]["count"] == 1


def test_new_entity_alerts_read_only_org_scoped(monkeypatch):
    from core import brand_registry_mcp as brm
    from core import tracked_entity_matching as match

    handlers = _handlers()
    monkeypatch.setattr(brm, "_identity", lambda: "user-1")
    monkeypatch.setattr(brm, "_guard_org_read", lambda org, ident: None)
    captured = {}
    monkeypatch.setattr(
        match,
        "list_open_alerts",
        lambda *, org_id: captured.update({"org_id": org_id}) or [{"id": "alert_1"}],
    )
    result = handlers["registry_new_entity_alerts"]("org-1")
    assert captured == {"org_id": "org-1"}
    assert result.structured_content["data"]["alerts"] == [{"id": "alert_1"}]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("registry_entities", ("   ",)),
        ("registry_entity_upsert", ("org-1", "  ")),
        ("registry_alias_add", ("tent_1", "org-1", "")),
        ("registry_wire_binding", ("tent_1", "org-1", "   ")),
        ("registry_role_set", ("tent_1", "proj-1", "org-1", "")),
        ("registry_alert_approve", ("alert_1", "org-1", "")),
    ],
)
def test_missing_param_before_guard_or_store(monkeypatch, tool, args):
    from core import brand_registry_mcp as brm

    handlers = _handlers()
    # A guard that would explode if reached -- proves the missing_param check runs FIRST.
    boom = MagicMock(side_effect=AssertionError("guard reached before param check"))
    monkeypatch.setattr(brm, "_identity", boom)
    monkeypatch.setattr(brm, "_guard_org_manage", boom)
    monkeypatch.setattr(brm, "_guard_org_read", boom)
    with pytest.raises(Exception) as exc:
        handlers[tool](*args)
    assert _err_code(exc.value) == "missing_param"


def test_no_provider_or_brand_vocabulary_ad2():
    """AD-2 (E40-NFR03): the module carries no provider/brand name and no role literal
    outside the ROLE_* constants imported from 40.1 + the decision literals."""
    import pathlib

    from core import brand_registry_mcp as brm

    src = pathlib.Path(brm.__file__).read_text(encoding="utf-8").lower()
    for provider in (
        "google-analytics",
        "meta-ads",
        "google-ads",
        "doubleverify",
        "pinterest",
        "twitter",
        "amazon-ads",
        "tiktok",
        "linkedin",
    ):
        assert provider not in src


def test_every_registration_is_governance_profile():
    """Static: NO tool leaks into a non-governance profile. The dynamic registration
    already proves every declaration is governance (test_every_tool_is_sensitive_governance);
    this guards the SOURCE so a future edit cannot register under insights/operations/support.
    """
    import pathlib

    from core import brand_registry_mcp as brm

    src = pathlib.Path(brm.__file__).read_text(encoding="utf-8")
    assert 'profile="insights"' not in src
    assert 'profile="operations"' not in src
    assert 'profile="support"' not in src
    # governance is the ONLY registration profile, and both read + write effects exist.
    # Whitespace-independent (ruff may wrap the register_profiled args across lines).
    assert 'profile="governance"' in src
    assert 'effect="read"' in src
    assert 'effect="write"' in src
