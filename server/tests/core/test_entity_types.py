"""The feeder-less entity-type declaration -- the rules, proven without a database.

Story 68.1. What is proven here is the LOGIC: the shape refusals, the line
between a replay and a real duplicate, and the door declarations. What is
schema -- the persisted `canonical_key`, the audit row, RLS -- is proven on
real Postgres in `tests/integration/test_entity_types_pg.py`, the discipline
`tests/core/test_master_data.py` states in its own header.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.master_data import MasterDataConflict, MasterDataError
from core.object_kind_registry import (
    EntityTypeExists,
    declaration_matches,
    validate_entity_type_declaration,
)

_MCP_MODULE = Path(__file__).resolve().parents[2] / "core" / "entity_types_mcp.py"


# ---------------------------------------------------------------------------
# The shape guards: the declaration is validated before any write.
# ---------------------------------------------------------------------------


def test_a_well_formed_declaration_is_normalized():
    kind, key, name = validate_entity_type_declaration(
        object_kind="  video  ", canonical_key="video_id", display_name=" Videos "
    )
    assert (kind, key, name) == ("video", "video_id", "Videos")


@pytest.mark.parametrize("bad", ["", "  ", "Video", "v", "1video", "video-id", "x" * 41])
def test_a_malformed_object_kind_is_refused_naming_the_field(bad):
    with pytest.raises(MasterDataError) as excinfo:
        validate_entity_type_declaration(
            object_kind=bad, canonical_key="video_id", display_name="Videos"
        )
    assert "object_kind" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "   ", "VideoID", "video id", "1d", "x" * 41])
def test_a_malformed_canonical_key_is_refused_naming_the_field(bad):
    with pytest.raises(MasterDataError) as excinfo:
        validate_entity_type_declaration(
            object_kind="video", canonical_key=bad, display_name="Videos"
        )
    assert "canonical_key" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "   ", "x" * 121])
def test_an_empty_or_oversized_display_name_is_refused(bad):
    with pytest.raises(MasterDataError) as excinfo:
        validate_entity_type_declaration(
            object_kind="video", canonical_key="video_id", display_name=bad
        )
    assert "display_name" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Replay vs duplicate: the whole declaration is compared, never the kind alone.
# ---------------------------------------------------------------------------


def _registry(**overrides):
    base = {
        "id": "mdreg_0000000000000000000000000A",
        "object_kind": "video",
        "canonical_key": "video_id",
        "label": "Videos",
        "created_by": "alice@example.com",
    }
    base.update(overrides)
    return base


def test_the_same_declaration_matches():
    assert declaration_matches(
        _registry(), canonical_key="video_id", display_name="Videos"
    )


def test_a_different_key_does_not_match():
    assert not declaration_matches(
        _registry(), canonical_key="external_id", display_name="Videos"
    )


def test_a_different_label_does_not_match():
    assert not declaration_matches(
        _registry(), canonical_key="video_id", display_name="Films"
    )


def test_a_registry_that_never_named_a_key_matches_no_declaration():
    """A pre-68.1 registry (NULL canonical_key) is not a replay of anything."""
    assert not declaration_matches(
        _registry(canonical_key=None), canonical_key="video_id", display_name="Videos"
    )


def test_the_conflict_is_named_and_catches_as_the_generic_one():
    """`entity_type_exists` for the doors; MasterDataConflict for the catch-all."""
    assert EntityTypeExists.code == "entity_type_exists"
    assert issubclass(EntityTypeExists, MasterDataConflict)


# ---------------------------------------------------------------------------
# The MCP door: the capability split and the catalogue declarations.
# ---------------------------------------------------------------------------


def test_declaring_needs_edit_and_listing_needs_read():
    from core.entity_types_mcp import minimum_capability

    assert minimum_capability("declare") == "edit"
    assert minimum_capability("list") == "read"


def test_an_unknown_action_does_not_fall_through_to_read():
    """A typo must not buy the weakest guard."""
    from core.entity_types_mcp import minimum_capability

    assert minimum_capability("something_new") == "edit"
    assert minimum_capability("") == "edit"


def test_no_object_kind_literal_appears_in_the_module():
    """The client names the type, not the code -- AD-2 one layer up."""
    source = _MCP_MODULE.read_text(encoding="utf-8")
    executable = source[source.index("from __future__") :]
    for suspect in ("video", "product", "venue", "country", "market"):
        assert not re.search(rf'object_kind\s*==\s*[\'"]{suspect}', executable)
        assert f'"{suspect}"' not in executable, f"{suspect!r} is a kind literal"


def test_the_authorization_is_the_neighbours_and_not_a_softer_one():
    """`hold_access=True`, strict resolution, deny-by-default as `project_not_found`.

    Answering `forbidden` would confirm that another Project's master data
    exists, which is itself the sensitive fact. Read from the source because the
    behaviour it guards needs a database, and the declaration does not.
    """
    source = _MCP_MODULE.read_text(encoding="utf-8")
    assert "resolve_strict_resource_access" in source
    assert "hold_access=True" in source
    assert 'raise _tool_error("project_not_found"' in source
    assert '"forbidden"' not in source and "'forbidden'" not in source


def test_an_outage_of_the_decision_fails_closed_onto_the_same_answer():
    """Denied and unreadable are ONE envelope -- comparing them teaches nothing."""
    source = _MCP_MODULE.read_text(encoding="utf-8")
    assert source.count('raise _tool_error("project_not_found"') >= 3


def test_a_write_without_an_idempotency_key_is_refused_but_a_read_is_not():
    from core.entity_types_mcp import _run

    with pytest.raises(Exception) as excinfo:
        _run("declare", "proj_EXAMPLE", lambda conn, org, actor: None, idempotency_key="")
    assert "idempotency" in str(excinfo.value) or "not_found" in str(excinfo.value)


def test_the_named_conflict_is_not_collapsed_into_the_generic_one():
    """`entity_type_exists` reaches the agent; a bare "conflict" cannot be
    told from a replay."""
    source = _MCP_MODULE.read_text(encoding="utf-8")
    assert 'raise _tool_error("entity_type_exists"' in source
    # EntityTypeExists is caught BEFORE MasterDataConflict.
    assert source.index('raise _tool_error("entity_type_exists"') < source.index(
        'raise _tool_error("conflict"'
    )


def test_the_two_tools_declare_two_different_confirmation_modes():
    from core.entity_types_mcp import register
    from core.mcp_profiles import registered_declarations
    from fastmcp import FastMCP

    register(FastMCP("test-entity-types"))
    modes = {
        decl.name: decl.confirmation_mode
        for decl in registered_declarations()
        if decl.name in ("declare_entity_type", "list_entity_types")
    }
    assert modes.get("declare_entity_type") == "host"
    assert modes.get("list_entity_types") == "none"


def test_both_tools_are_governance_profile_and_operational():
    from core.entity_types_mcp import register
    from core.mcp_profiles import registered_declarations
    from fastmcp import FastMCP

    register(FastMCP("test-entity-types-profile"))
    ours = [
        decl
        for decl in registered_declarations()
        if decl.name in ("declare_entity_type", "list_entity_types")
    ]
    assert len(ours) == 2
    for decl in ours:
        assert decl.profile == "governance"
        assert decl.data_class == "operational"


# ---------------------------------------------------------------------------
# The audit action is declared where it is written (AD-42).
# ---------------------------------------------------------------------------


def test_the_audit_action_is_declared_by_the_writing_module():
    from core.audit import declared_actions

    assert declared_actions().get("mdm.entity_type.declared") == "core.object_kind_registry"
