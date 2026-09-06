"""MCP lifecycle parity for the Project capability surface (Story 48.1).

What these tests hold to:

* the effect taxonomy is exactly ``read | prepare | confirmed_write``, and the
  declarations that were generic ``write`` now say which they are;
* a ``prepare`` cannot be declared as authorizing, and a ``confirmed_write``
  cannot be declared without a trusted confirmation;
* a confirmed write is neither discoverable nor directly callable without proven
  interactive presence;
* REST and MCP produce the same impact payload, byte for byte;
* no confirmation material appears in anything a model can see.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from core import mcp_profiles
from core.mcp_profiles import (
    EFFECTS,
    CatalogValidationError,
    ToolDeclaration,
    interactive_presence_verified,
    register_profiled,
    registered_declarations,
    reset_registry_for_tests,
)

_MODULE = Path(__file__).resolve().parents[2] / "core" / "project_capabilities_mcp.py"


class _Recorder:
    """Minimal FastMCP stand-in: records what a module registers."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def tool(self, handler, *, name=None, tags=None, meta=None):
        self.tools[name or handler.__name__] = {"handler": handler, "tags": tags, "meta": meta}
        return handler


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# The taxonomy itself.
# ---------------------------------------------------------------------------


def test_the_effect_taxonomy_is_exactly_the_ratified_three() -> None:
    assert EFFECTS == ("read", "prepare", "confirmed_write")
    assert "write" not in EFFECTS


def test_the_obsolete_generic_write_effect_is_gone_from_the_whole_catalog() -> None:
    # The class, not the instance: no module may still declare the old effect.
    core = Path(__file__).resolve().parents[2] / "core"
    offenders = [
        path.name
        for path in core.glob("*.py")
        if 'effect="write"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_a_prepare_cannot_be_declared_as_authorizing() -> None:
    recorder = _Recorder()

    def prepare_something():
        return None

    # `none` and server-side verification are both non-authorizing, so both pass.
    for mode in ("none", "server"):
        reset_registry_for_tests()
        register_profiled(
            recorder,
            prepare_something,
            profile="operations",
            effect="prepare",
            data_class="operational",
            confirmation_mode=mode,
        )
    # A human or in-app approval would make it authorize, which a prepare must not.
    for mode in ("human", "host"):
        reset_registry_for_tests()
        with pytest.raises(CatalogValidationError, match="cannot authorize"):
            register_profiled(
                recorder,
                prepare_something,
                profile="operations",
                effect="prepare",
                data_class="operational",
                confirmation_mode=mode,
            )


def test_a_confirmed_write_cannot_be_declared_without_a_trusted_confirmation() -> None:
    recorder = _Recorder()

    def mutate_something():
        return None

    for mode in ("none", "server"):
        reset_registry_for_tests()
        with pytest.raises(CatalogValidationError, match="requires a trusted confirmation"):
            register_profiled(
                recorder,
                mutate_something,
                profile="governance",
                effect="confirmed_write",
                data_class="sensitive",
                confirmation_mode=mode,
            )


def test_insights_stays_read_only_under_the_new_taxonomy() -> None:
    recorder = _Recorder()

    def peek():
        return None

    with pytest.raises(CatalogValidationError, match="safe reads only"):
        register_profiled(
            recorder,
            peek,
            profile="insights",
            effect="confirmed_write",
            data_class="public",
            confirmation_mode="human",
        )


# ---------------------------------------------------------------------------
# The four capability tools.
# ---------------------------------------------------------------------------


def _register() -> _Recorder:
    from core.project_capabilities_mcp import register

    recorder = _Recorder()
    register(recorder)
    return recorder


def test_the_six_capability_operations_declare_the_exact_metadata() -> None:
    """The four of story 48.1, plus the row-decision pair of story 71.4.

    The pair is listed HERE rather than in a file of its own because this is the
    catalog assertion: a set comparison is what catches a seventh tool arriving
    without a decision, and two of them would each pass while the union grew.
    """
    _register()
    declarations = {decl.name: decl for decl in registered_declarations()}
    assert set(declarations) == {
        "read_project_capability",
        "preview_project_capability_impact",
        "prepare_project_capability_change",
        "confirm_project_capability_change",
        # Story 71.4 -- ONE ROW of a capability, never the capability's activation.
        # Generic in shape: they take a `capability_key` and dispatch.
        "prepare_project_capability_row_decision",
        "confirm_project_capability_row_decision",
    }
    assert declarations["read_project_capability"].effect == "read"
    assert declarations["preview_project_capability_impact"].effect == "read"
    assert declarations["prepare_project_capability_change"].effect == "prepare"
    assert declarations["confirm_project_capability_change"].effect == "confirmed_write"
    assert declarations["prepare_project_capability_row_decision"].effect == "prepare"
    assert declarations["confirm_project_capability_row_decision"].effect == "confirmed_write"
    for decl in declarations.values():
        # AD-24 requires the full tuple on every contract, not a partial one.
        assert decl.profile == "governance"
        assert decl.data_class in {"operational", "sensitive"}
        assert decl.confirmation_mode in {"none", "human"}


def test_interactive_presence_needs_server_minted_evidence_not_a_host_claim() -> None:
    # 67-16: `attested_context_id` is set only by `_capability_context`, from a live
    # app.mcp_capability_contexts row. Without it these grants are a host claim.
    endpoint = {
        "endpoint_binding": "endpoint-1",
        "workspace_evidence_hash": "a" * 64,
        "attested_context_id": "mcpctx_TESTATTESTED",
    }
    # A host asserting it is interactive proves nothing.
    assert interactive_presence_verified({**endpoint, "interactive": True}) is False
    assert interactive_presence_verified({**endpoint}) is False
    assert interactive_presence_verified({"interactive_presence_evidence_hash": "b" * 64}) is False
    # An unattested claim carrying a perfect presence hash proves nothing either.
    assert (
        interactive_presence_verified(
            {
                "endpoint_binding": "endpoint-1",
                "workspace_evidence_hash": "a" * 64,
                "interactive_presence_evidence_hash": "b" * 64,
            }
        )
        is False
    )
    assert (
        interactive_presence_verified(
            {**endpoint, "interactive_presence_evidence_hash": "b" * 64}
        )
        is True
    )
    # Shape is checked, not merely presence.
    assert (
        interactive_presence_verified({**endpoint, "interactive_presence_evidence_hash": "zz"})
        is False
    )


def test_a_confirmed_write_is_hidden_and_denied_without_proven_presence(monkeypatch) -> None:
    _register()
    middleware = mcp_profiles.build_middleware()
    if middleware is None:  # pragma: no cover -- FastMCP absent in this environment
        pytest.skip("FastMCP middleware base unavailable")

    class _Tool:
        def __init__(self, name: str, effect: str) -> None:
            self.name = name
            self.meta = {"profile": "governance", "effect": effect}
            self.tags = {"profile:governance", f"effect:{effect}"}

    tools = [
        _Tool("preview_project_capability_impact", "read"),
        _Tool("confirm_project_capability_change", "confirmed_write"),
    ]

    def _context(with_presence: bool):
        grants = {
            # 67-16: attested against the live capability-context row.
            "attested_context_id": "mcpctx_TESTATTESTED",
            "enabled_profiles": ["governance"],
            "endpoint_binding": "endpoint-1",
            "workspace_evidence_hash": "a" * 64,
        }
        if with_presence:
            grants["interactive_presence_evidence_hash"] = "b" * 64
        return ("person-1", {"host": "opaque"}, grants)


    async def call_next(_context):
        return tools

    import asyncio

    # Without presence: the read survives discovery, the confirmed write does not.
    monkeypatch.setattr(mcp_profiles, "_capability_context", lambda: _context(False))
    visible = asyncio.run(middleware.on_list_tools(None, call_next))
    assert [tool.name for tool in visible] == ["preview_project_capability_impact"]

    # And a direct call to the hidden tool is refused, without disclosing it exists.
    class _Message:
        name = "confirm_project_capability_change"

    class _Ctx:
        message = _Message()

    async def passthrough(_context):
        return "executed"

    with pytest.raises(Exception) as refusal:
        asyncio.run(middleware.on_call_tool(_Ctx(), passthrough))
    assert "not_found" in str(refusal.value)

    # With server-minted presence, both are available.
    monkeypatch.setattr(mcp_profiles, "_capability_context", lambda: _context(True))
    visible = asyncio.run(middleware.on_list_tools(None, call_next))
    assert len(visible) == 2
    assert asyncio.run(middleware.on_call_tool(_Ctx(), passthrough)) == "executed"


# ---------------------------------------------------------------------------
# Parity and secrecy, asserted on the code rather than described in prose.
# ---------------------------------------------------------------------------


def test_mcp_and_rest_share_the_same_handlers_and_serializer() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    # The MCP surface delegates; it owns no store and no coverage arithmetic.
    assert "from core.capability_proposals import read_project_capability" in source
    assert "from core.project_settings import read_change_set_impact" in source
    assert "aggregate_coverage" not in source
    assert "INSERT INTO app.datastream_capability_proposals" not in source


def test_equivalent_requests_produce_the_same_impact_hash() -> None:
    from core.capability_proposals import canonical_hash as proposal_hash
    from core.project_settings import canonical_hash as settings_hash

    # One canonical hash, used by both sides of the parity claim: a divergence here
    # would let REST and MCP disagree about what a human approved.
    payload = {"coverage": {"applicable": 2, "complete": 1}, "matrix": [{"a": 1}]}
    assert proposal_hash(payload) == settings_hash(payload)
    assert proposal_hash(payload) == proposal_hash(json.loads(json.dumps(payload)))


def test_the_confirm_tool_accepts_no_confirmation_material() -> None:
    from core.project_capabilities_mcp import register

    recorder = _Recorder()
    register(recorder)
    signature = inspect.signature(
        recorder.tools["confirm_project_capability_change"]["handler"]
    )
    parameters = set(signature.parameters)
    # Nothing a host passes could avoid landing in model-visible tool arguments,
    # so the tool takes no secret, no token and no confirmation id at all.
    assert parameters == {"project_id", "change_set_id", "review_reference", "idempotency_key"}
    assert not any("secret" in name or "token" in name for name in parameters)


def test_no_confirmation_material_can_reach_a_model_visible_channel() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    # The secret never appears in a returned envelope. `_envelope` is the only
    # place model-visible content is built, so this scan is exhaustive by design.
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("*"):
            continue
        if "confirmation_secret" in stripped:
            # The single permitted use: naming the domain keyword as absent.
            assert "confirmation_secret=None" in stripped, stripped


def test_a_non_interactive_host_is_told_where_to_go_instead() -> None:
    from core.project_capabilities_mcp import console_deep_link

    link = console_deep_link("proj_EXAMPLE", section="changes", change_set_id="pcset_EXAMPLE")
    assert link["requires_authenticated_session"] is True
    owner = link["owner_reference"]
    # A semantic reference to the Settings surface, not a hard-coded path.
    assert owner["global_surface"] == "project-settings"
    assert owner["global_section"] == "changes"
    assert not any(isinstance(value, str) and value.startswith("http") for value in owner.values())


def test_the_bounded_payload_says_what_it_dropped() -> None:
    from core.project_capabilities_mcp import _bounded_matrix

    rows = [{"datastream_id": f"ds_{index}"} for index in range(80)]
    bounded, withheld = _bounded_matrix(rows)
    # A silent truncation reads as "covered everything"; it must not be silent.
    assert len(bounded) + withheld == len(rows)
    assert withheld > 0
    assert _bounded_matrix(rows[:3]) == (rows[:3], 0)


def test_a_declaration_is_immutable_once_recorded() -> None:
    recorder = _Recorder()

    def tool_one():
        return None

    register_profiled(
        recorder,
        tool_one,
        profile="governance",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    with pytest.raises(CatalogValidationError, match="different capability profile"):
        register_profiled(
            recorder,
            tool_one,
            profile="governance",
            effect="confirmed_write",
            data_class="sensitive",
            confirmation_mode="human",
        )
    assert ToolDeclaration in ToolDeclaration.__mro__
